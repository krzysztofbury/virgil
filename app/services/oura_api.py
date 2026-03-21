import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode

import httpx

from app.services.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)

OURA_AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
OURA_TOKEN_URL = "https://api.ouraring.com/oauth/token"
OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"
OURA_WEBHOOK_URL = "https://api.ouraring.com/v2/webhook/subscription"
OURA_SCOPES = "daily heartrate session spo2 sleep workout"


def get_oura_auth_url(client_id: str, redirect_uri: str, state: str = "") -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": OURA_SCOPES,
    }
    if state:
        params["state"] = state
    return f"{OURA_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OURA_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OURA_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def ensure_valid_token(db) -> str | None:
    """Check token expiry, auto-refresh if needed, return decrypted access_token or None."""
    row = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
    if not row:
        return None
    integration = dict(row[0])
    if integration["status"] != "connected":
        return None

    access_token = decrypt(integration["access_token_enc"])
    if not access_token:
        return None

    # Check expiry
    expires_at = integration.get("token_expires_at", "")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            if now >= exp_dt - timedelta(minutes=5):
                # Refresh
                client_id = integration["client_id"]
                client_secret = decrypt(integration["client_secret_enc"])
                refresh_tok = decrypt(integration["refresh_token_enc"])
                if not refresh_tok:
                    return None
                try:
                    tokens = await refresh_access_token(client_id, client_secret, refresh_tok)
                    new_access = tokens["access_token"]
                    new_refresh = tokens.get("refresh_token", refresh_tok)
                    expires_in = tokens.get("expires_in", 86400)
                    new_expires = (now + timedelta(seconds=expires_in)).isoformat()
                    await db.execute(
                        """UPDATE integrations SET access_token_enc = ?, refresh_token_enc = ?,
                           token_expires_at = ? WHERE provider = 'oura'""",
                        (encrypt(new_access), encrypt(new_refresh), new_expires),
                    )
                    await db.commit()
                    return new_access
                except Exception:
                    logger.exception("Failed to refresh Oura token")
                    await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
                    await db.commit()
                    return None
        except (ValueError, TypeError):
            # Corrupted expiry timestamp — treat as expired, return token as-is
            # since we cannot determine if it is still valid.
            logger.warning("Could not parse token_expires_at=%r, using token as-is", expires_at)

    return access_token


class OuraAuthError(Exception):
    """Raised when Oura API returns 401 — token is expired or revoked."""


async def _fetch_endpoint(
    client: httpx.AsyncClient, endpoint: str, token: str, start: str, end: str, max_retries: int = 3
) -> list:
    for attempt in range(max_retries + 1):
        resp = await client.get(
            f"{OURA_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            params={"start_date": start, "end_date": end},
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
        if resp.status_code == 401:
            raise OuraAuthError(f"Oura API {endpoint} returned 401 — token expired or revoked")
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
            logger.warning("Oura API %s rate limited, retrying in %ds", endpoint, retry_after)
            await asyncio.sleep(retry_after)
            continue
        logger.warning("Oura API %s returned %s: %s", endpoint, resp.status_code, resp.text[:200])
        return []
    return []


async def fetch_oura_daily(access_token: str, start_date: str, end_date: str) -> dict[str, dict]:
    """Fetch multiple Oura endpoints and merge into per-day dicts."""
    daily: dict[str, dict] = defaultdict(dict)

    async with httpx.AsyncClient(timeout=30.0) as client:
        sleep_data = await _fetch_endpoint(client, "daily_sleep", access_token, start_date, end_date)
        for item in sleep_data:
            d = item.get("day", "")
            if d:
                daily[d]["sleep_score"] = item.get("score")

        readiness_data = await _fetch_endpoint(client, "daily_readiness", access_token, start_date, end_date)
        for item in readiness_data:
            d = item.get("day", "")
            if d:
                daily[d]["readiness_score"] = item.get("score")

        activity_data = await _fetch_endpoint(client, "daily_activity", access_token, start_date, end_date)
        for item in activity_data:
            d = item.get("day", "")
            if d:
                daily[d]["activity_score"] = item.get("score")
                daily[d]["steps"] = item.get("steps")

        stress_data = await _fetch_endpoint(client, "daily_stress", access_token, start_date, end_date)
        for item in stress_data:
            d = item.get("day", "")
            if d:
                summary = (
                    item.get("stress_high", 0),
                    item.get("stress_medium", 0),
                    item.get("stress_low", 0),
                    item.get("recovery_high", 0),
                )
                daily[d]["stress_high"] = summary[0]
                daily[d]["stress_medium"] = summary[1]
                daily[d]["stress_low"] = summary[2]
                daily[d]["stress_rest"] = summary[3]

        # Sleep periods for duration/deep/REM breakdown
        sleep_periods = await _fetch_endpoint(client, "sleep", access_token, start_date, end_date)
        # Group by day, pick longest period per day
        sleep_by_day: dict[str, dict] = {}
        for item in sleep_periods:
            d = item.get("day", "")
            if not d:
                continue
            duration = item.get("total_sleep_duration", 0) or 0
            if d not in sleep_by_day or duration > (sleep_by_day[d].get("total_sleep_duration", 0) or 0):
                sleep_by_day[d] = item
        for d, item in sleep_by_day.items():
            total_sec = item.get("total_sleep_duration", 0) or 0
            deep_sec = item.get("deep_sleep_duration", 0) or 0
            rem_sec = item.get("rem_sleep_duration", 0) or 0
            daily[d]["sleep_duration_hours"] = round(total_sec / 3600, 2) if total_sec else None
            daily[d]["deep_sleep_hours"] = round(deep_sec / 3600, 2) if deep_sec else None
            daily[d]["rem_sleep_hours"] = round(rem_sec / 3600, 2) if rem_sec else None
            daily[d]["lowest_hr"] = item.get("lowest_heart_rate")
            daily[d]["avg_hrv"] = item.get("average_hrv")

        # Heartrate for resting HR — use daily average of lowest readings
        hr_data = await _fetch_endpoint(client, "heartrate", access_token, start_date, end_date)
        hr_by_day: dict[str, list] = defaultdict(list)
        for item in hr_data:
            ts = item.get("timestamp", "")
            if ts and len(ts) >= 10:
                d = ts[:10]
                bpm = item.get("bpm")
                if bpm:
                    hr_by_day[d].append(bpm)
        for d, bpms in hr_by_day.items():
            if bpms:
                # Use the lowest 10% as resting HR estimate
                sorted_bpms = sorted(bpms)
                count = max(1, len(sorted_bpms) // 10)
                daily[d].setdefault("resting_hr", round(sum(sorted_bpms[:count]) / count, 1))

    return dict(daily)


async def fetch_oura_workouts(access_token: str, start_date: str, end_date: str) -> list[dict]:
    """Fetch Oura workout data and return normalized dicts."""
    workouts = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        raw = await _fetch_endpoint(client, "workout", access_token, start_date, end_date)
    for item in raw:
        oura_id = item.get("id", "")
        day = item.get("day", "")
        if not oura_id or not day:
            continue
        # Compute duration from start/end datetimes
        start_dt = item.get("start_datetime", "")
        end_dt = item.get("end_datetime", "")
        duration = 0
        if start_dt and end_dt:
            try:
                s = datetime.fromisoformat(start_dt)
                e = datetime.fromisoformat(end_dt)
                duration = max(0, int((e - s).total_seconds() / 60))
            except (ValueError, TypeError):
                pass
        workouts.append(
            {
                "oura_id": oura_id,
                "date": day,
                "activity": item.get("activity", "unknown"),
                "duration_minutes": duration,
                "calories": item.get("calories"),
                "distance_meters": item.get("distance"),
                "intensity": item.get("intensity", ""),
                "start_datetime": start_dt,
                "end_datetime": end_dt,
            }
        )
    return workouts


async def _auto_populate_experiments(db):
    """Push oura_workouts into experiment_entries for active experiments with source_match configured."""
    exp_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE status = 'active'")
    for row in exp_rows:
        exp = dict(row)
        start = date.fromisoformat(exp["start_date"])
        end = start + timedelta(weeks=exp["num_weeks"])

        # Get activity types with source_match configured
        type_rows = await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types WHERE experiment_id = ? AND source_match != ''",
            (exp["id"],),
        )
        if not type_rows:
            continue

        # Build mapping: oura activity name → activity_type_id
        match_map: dict[str, int] = {}
        for tr in type_rows:
            at = dict(tr)
            for name in at["source_match"].split(","):
                name = name.strip().lower()
                if name:
                    match_map[name] = at["id"]

        # Get Oura workouts in the experiment date range
        workout_rows = await db.execute_fetchall(
            "SELECT * FROM oura_workouts WHERE date >= ? AND date < ?",
            (start.isoformat(), end.isoformat()),
        )

        for wr in workout_rows:
            w = dict(wr)
            activity_lower = w["activity"].lower()
            type_id = match_map.get(activity_lower)
            if type_id is None:
                continue
            notes = f"Oura: {w['activity']}"
            if w.get("intensity"):
                notes += f" ({w['intensity']})"
            await db.execute(
                "INSERT OR IGNORE INTO experiment_entries "
                "(experiment_id, date, activity_type_id, duration_minutes, notes, source, source_ref) "
                "VALUES (?, ?, ?, ?, ?, 'oura', ?)",
                (exp["id"], w["date"], type_id, w["duration_minutes"], notes, w["oura_id"]),
            )


async def sync_oura_from_api(db, days_back: int = 30) -> int:
    """Full sync pipeline: ensure token → fetch → upsert oura_daily → recompute oura_monthly."""
    token = await ensure_valid_token(db)
    if not token:
        raise RuntimeError("No valid Oura token available")

    end = date.today()
    start = end - timedelta(days=days_back)
    try:
        daily = await fetch_oura_daily(token, start.isoformat(), end.isoformat())
    except OuraAuthError:
        logger.warning("Oura token rejected during sync — marking integration as error")
        await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
        await db.commit()
        raise

    count = 0
    affected_months = set()
    for day_str, data in daily.items():
        await db.execute(
            """INSERT INTO oura_daily (date, sleep_score, readiness_score, activity_score, steps,
                sleep_duration_hours, deep_sleep_hours, rem_sleep_hours, resting_hr, lowest_hr,
                avg_hrv, stress_high, stress_medium, stress_low, stress_rest)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                sleep_score=excluded.sleep_score, readiness_score=excluded.readiness_score,
                activity_score=excluded.activity_score, steps=excluded.steps,
                sleep_duration_hours=excluded.sleep_duration_hours, deep_sleep_hours=excluded.deep_sleep_hours,
                rem_sleep_hours=excluded.rem_sleep_hours, resting_hr=excluded.resting_hr,
                lowest_hr=excluded.lowest_hr, avg_hrv=excluded.avg_hrv,
                stress_high=excluded.stress_high, stress_medium=excluded.stress_medium,
                stress_low=excluded.stress_low, stress_rest=excluded.stress_rest""",
            (
                day_str,
                data.get("sleep_score"),
                data.get("readiness_score"),
                data.get("activity_score"),
                data.get("steps"),
                data.get("sleep_duration_hours"),
                data.get("deep_sleep_hours"),
                data.get("rem_sleep_hours"),
                data.get("resting_hr"),
                data.get("lowest_hr"),
                data.get("avg_hrv"),
                data.get("stress_high"),
                data.get("stress_medium"),
                data.get("stress_low"),
                data.get("stress_rest"),
            ),
        )
        count += 1
        affected_months.add(day_str[:7])

    # Recompute oura_monthly for affected months
    for month in affected_months:
        rows = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date LIKE ?", (f"{month}%",))
        if not rows:
            continue
        days = [dict(r) for r in rows]
        n = len(days)

        def avg(field, _days=days):
            vals = [d[field] for d in _days if d.get(field) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def total(field, _days=days):
            vals = [d[field] for d in _days if d.get(field) is not None]
            return sum(vals) if vals else None

        steps_total = total("steps")
        steps_avg = steps_total // n if steps_total else None

        # Oura API v2 daily_stress only provides stress_high (seconds) and
        # stress_rest/recovery_high (seconds). stress_medium and stress_low
        # are always 0 — those fields don't exist in the API.
        # Monthly mapping: stress_stressful = stress_high, stress_restored = stress_rest.
        # stress_normal is kept for schema compatibility but will always be 0.
        await db.execute(
            """INSERT INTO oura_monthly (month, sleep_score, readiness, activity, steps,
                sleep_duration, deep_sleep, rem_sleep, rhr, lowest_hr, hrv,
                stress_normal, stress_stressful, stress_restored)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month) DO UPDATE SET
                sleep_score=excluded.sleep_score, readiness=excluded.readiness,
                activity=excluded.activity, steps=excluded.steps,
                sleep_duration=excluded.sleep_duration, deep_sleep=excluded.deep_sleep,
                rem_sleep=excluded.rem_sleep, rhr=excluded.rhr, lowest_hr=excluded.lowest_hr,
                hrv=excluded.hrv, stress_normal=excluded.stress_normal,
                stress_stressful=excluded.stress_stressful, stress_restored=excluded.stress_restored""",
            (
                month,
                avg("sleep_score"),
                avg("readiness_score"),
                avg("activity_score"),
                steps_avg,
                avg("sleep_duration_hours"),
                avg("deep_sleep_hours"),
                avg("rem_sleep_hours"),
                avg("resting_hr"),
                avg("lowest_hr"),
                avg("avg_hrv"),
                0,  # stress_normal: always 0 (stress_low doesn't exist in Oura API v2)
                total("stress_high"),  # stress_stressful = high stress seconds
                total("stress_rest"),  # stress_restored = recovery seconds
            ),
        )

    # Sync workouts
    try:
        workouts = await fetch_oura_workouts(token, start.isoformat(), end.isoformat())
        for w in workouts:
            await db.execute(
                """INSERT INTO oura_workouts (date, activity, duration_minutes, calories,
                    distance_meters, intensity, start_datetime, end_datetime, oura_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(oura_id) DO UPDATE SET
                    date=excluded.date, activity=excluded.activity,
                    duration_minutes=excluded.duration_minutes, calories=excluded.calories,
                    distance_meters=excluded.distance_meters, intensity=excluded.intensity,
                    start_datetime=excluded.start_datetime, end_datetime=excluded.end_datetime""",
                (
                    w["date"],
                    w["activity"],
                    w["duration_minutes"],
                    w["calories"],
                    w["distance_meters"],
                    w["intensity"],
                    w["start_datetime"],
                    w["end_datetime"],
                    w["oura_id"],
                ),
            )
        await _auto_populate_experiments(db)
    except OuraAuthError:
        logger.warning("Oura workout scope not authorized — skipping workout sync")
    except Exception:
        logger.exception("Failed to sync Oura workouts")

    # Update last_sync_at
    now = datetime.now(UTC).isoformat()
    await db.execute("UPDATE integrations SET last_sync_at = ? WHERE provider = 'oura'", (now,))
    await db.commit()
    return count


# ── Webhook Subscription API ──


async def create_webhook_subscription(
    access_token: str, callback_url: str, verification_token: str, event_type: str = "tag.updated"
) -> dict:
    """Register a webhook subscription with Oura API v2.

    Oura will POST a verification request to callback_url with the verification_token.
    The callback must respond with the token to complete registration.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            OURA_WEBHOOK_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "callback_url": callback_url,
                "verification_token": verification_token,
                "event_type": event_type,
                "data_type": "tag",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def delete_webhook_subscription(access_token: str, subscription_id: str) -> None:
    """Delete a webhook subscription from Oura API v2."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{OURA_WEBHOOK_URL}/{subscription_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()


async def list_webhook_subscriptions(access_token: str) -> list[dict]:
    """List all active webhook subscriptions."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            OURA_WEBHOOK_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()
