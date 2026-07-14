import asyncio
import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
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


class OuraFetchError(Exception):
    """Raised when an Oura endpoint fails after retries (non-auth failure)."""


# Which oura_daily columns each endpoint owns. On a partial sync failure, only
# columns from endpoints that actually succeeded are updated — otherwise a
# transient 5xx would overwrite historic values with NULL.
ENDPOINT_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_sleep": ("sleep_score",),
    "daily_readiness": ("readiness_score",),
    "daily_activity": ("activity_score", "steps"),
    "daily_stress": ("stress_high", "stress_medium", "stress_low", "stress_rest"),
    "sleep": ("sleep_duration_hours", "deep_sleep_hours", "rem_sleep_hours", "lowest_hr", "avg_hrv"),
    "heartrate": ("resting_hr",),
}
DAILY_ENDPOINT_ORDER = ("daily_sleep", "daily_readiness", "daily_activity", "daily_stress", "sleep", "heartrate")


# Retry-After can be server-controlled and may legally be an HTTP-date — never
# sleep unbounded on it, and never let its parsing crash the sync.
RETRY_AFTER_SECONDS_MAX = 60


def _parse_retry_after(header_value: str | None, fallback: int) -> int:
    """Parse a Retry-After header into a bounded sleep in seconds."""
    try:
        seconds = int(header_value) if header_value else fallback
    except (ValueError, TypeError):
        seconds = fallback
    return max(1, min(RETRY_AFTER_SECONDS_MAX, seconds))


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
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"), fallback=2 ** (attempt + 1))
            logger.warning("Oura API %s rate limited, retrying in %ds", endpoint, retry_after)
            await asyncio.sleep(retry_after)
            continue
        logger.warning("Oura API %s returned %s: %s", endpoint, resp.status_code, resp.text[:200])
        raise OuraFetchError(f"Oura API {endpoint} failed with status {resp.status_code}")
    raise OuraFetchError(f"Oura API {endpoint} still rate limited after {max_retries} retries")


async def _fetch_optional(
    client: httpx.AsyncClient, endpoint: str, token: str, start: str, end: str, ok_endpoints: set[str]
) -> list:
    """Fetch one endpoint; record success in ok_endpoints, absorb fetch failures.

    Auth errors still propagate — a dead token must fail the whole sync.
    """
    try:
        data = await _fetch_endpoint(client, endpoint, token, start, end)
    except OuraFetchError:
        return []
    ok_endpoints.add(endpoint)
    return data


async def fetch_oura_daily(access_token: str, start_date: str, end_date: str) -> tuple[dict[str, dict], set[str]]:
    """Fetch multiple Oura endpoints and merge into per-day dicts.

    Returns (daily_data, ok_endpoints). Endpoints missing from ok_endpoints
    failed and their columns must not be written over existing data.
    """
    daily: dict[str, dict] = defaultdict(dict)
    ok_endpoints: set[str] = set()

    async with httpx.AsyncClient(timeout=30.0) as client:
        sleep_data = await _fetch_optional(client, "daily_sleep", access_token, start_date, end_date, ok_endpoints)
        for item in sleep_data:
            d = item.get("day", "")
            if d:
                daily[d]["sleep_score"] = item.get("score")

        readiness_data = await _fetch_optional(
            client, "daily_readiness", access_token, start_date, end_date, ok_endpoints
        )
        for item in readiness_data:
            d = item.get("day", "")
            if d:
                daily[d]["readiness_score"] = item.get("score")

        activity_data = await _fetch_optional(
            client, "daily_activity", access_token, start_date, end_date, ok_endpoints
        )
        for item in activity_data:
            d = item.get("day", "")
            if d:
                daily[d]["activity_score"] = item.get("score")
                daily[d]["steps"] = item.get("steps")

        stress_data = await _fetch_optional(client, "daily_stress", access_token, start_date, end_date, ok_endpoints)
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
        sleep_periods = await _fetch_optional(client, "sleep", access_token, start_date, end_date, ok_endpoints)
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
        hr_data = await _fetch_optional(client, "heartrate", access_token, start_date, end_date, ok_endpoints)
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

    return dict(daily), ok_endpoints


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


# All oura_daily data columns in one fixed order (identifier whitelist).
_ALL_DAILY_COLUMNS: tuple[str, ...] = tuple(col for ep in DAILY_ENDPOINT_ORDER for col in ENDPOINT_COLUMNS[ep])


@lru_cache(maxsize=64)  # bounded: at most 2^6 endpoint subsets exist
def _daily_upsert_sql_cached(ok_key: tuple[str, ...]) -> str:
    update_columns = [col for ep in DAILY_ENDPOINT_ORDER if ep in ok_key for col in ENDPOINT_COLUMNS[ep]]
    assert update_columns, f"No known endpoints in {ok_key}"

    insert_cols = ", ".join(["date", *_ALL_DAILY_COLUMNS])
    placeholders = ", ".join("?" * (len(_ALL_DAILY_COLUMNS) + 1))
    set_clause = ", ".join(f"{col}=excluded.{col}" for col in update_columns)
    return (
        f"INSERT INTO oura_daily ({insert_cols}) VALUES ({placeholders}) ON CONFLICT(date) DO UPDATE SET {set_clause}"
    )


def _daily_upsert_sql(ok_endpoints: set[str]) -> str:
    """Build the oura_daily upsert restricted to columns whose endpoints succeeded.

    All identifiers come from the hardcoded ENDPOINT_COLUMNS whitelist — values
    are still bound via ? placeholders. Cached per endpoint subset so the sync
    loop doesn't rebuild an identical string for every day.
    """
    assert ok_endpoints, "Refusing to build an upsert with no successful endpoints"
    return _daily_upsert_sql_cached(tuple(sorted(ok_endpoints)))


async def _upsert_daily(db, day_str: str, data: dict, ok_endpoints: set[str]) -> None:
    """Upsert one day of Oura data, preserving columns from failed endpoints."""
    values = [day_str] + [data.get(col) for col in _ALL_DAILY_COLUMNS]
    await db.execute(_daily_upsert_sql(ok_endpoints), values)


async def sync_oura_from_api(db, days_back: int = 30) -> int:
    """Full sync pipeline: ensure token → fetch → upsert oura_daily → recompute oura_monthly.

    Partial endpoint failures update only the successfully fetched columns;
    a total failure raises instead of committing destructive NULLs.
    """
    token = await ensure_valid_token(db)
    if not token:
        raise RuntimeError("No valid Oura token available")

    end = date.today()
    start = end - timedelta(days=days_back)
    try:
        daily, ok_endpoints = await fetch_oura_daily(token, start.isoformat(), end.isoformat())
    except OuraAuthError:
        logger.warning("Oura token rejected during sync — marking integration as error")
        await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
        await db.commit()
        raise

    if not ok_endpoints:
        raise RuntimeError("All Oura endpoints failed — nothing synced")
    failed = set(DAILY_ENDPOINT_ORDER) - ok_endpoints
    if failed:
        logger.warning("Oura sync partial: endpoints failed, preserving their columns: %s", sorted(failed))

    count = 0
    affected_months = set()
    for day_str, data in daily.items():
        await _upsert_daily(db, day_str, data, ok_endpoints)
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


# Data types we subscribe to — must stay in sync with the webhook handler's
# SUPPORTED_DATA_TYPES (app/routers/oura_webhook.py). Any incoming event just
# triggers a 2-day resync, so create+update cover all we need.
WEBHOOK_DATA_TYPES = ("daily_sleep", "daily_readiness", "daily_activity", "daily_stress", "sleep", "workout")
WEBHOOK_EVENT_TYPES = ("create", "update")


def _webhook_auth_headers(client_id: str, client_secret: str) -> dict[str, str]:
    """Oura's /v2/webhook/subscription endpoints authenticate with the OAuth
    APP credentials (x-client-id / x-client-secret headers), NOT a user Bearer
    token — per the ClientIdAuth/ClientSecretAuth security schemes in the
    Oura OpenAPI spec."""
    return {"x-client-id": client_id, "x-client-secret": client_secret}


async def create_webhook_subscription(
    client_id: str, client_secret: str, callback_url: str, verification_token: str
) -> dict:
    """Register one Oura subscription per (event_type, data_type) we handle.

    Oura API v2 requires a separate subscription per pair. During each
    registration Oura sends a verification GET to callback_url with
    ?verification_token=...&challenge=... and expects {"challenge": ...} back
    (handled in app/routers/oura_webhook.py).

    Returns {"created": [...ids...], "failed": [(event, data, error), ...]}.
    Raises if EVERY subscription failed.
    """
    created: list[str] = []
    failed: list[tuple[str, str, str]] = []
    headers = _webhook_auth_headers(client_id, client_secret)
    async with httpx.AsyncClient(timeout=30.0) as client:
        for data_type in WEBHOOK_DATA_TYPES:
            for event_type in WEBHOOK_EVENT_TYPES:
                try:
                    resp = await client.post(
                        OURA_WEBHOOK_URL,
                        headers=headers,
                        json={
                            "callback_url": callback_url,
                            "verification_token": verification_token,
                            "event_type": event_type,
                            "data_type": data_type,
                        },
                    )
                    resp.raise_for_status()
                    created.append(str(resp.json().get("id", "")))
                except httpx.HTTPStatusError as exc:
                    # Oura's response body says WHY (duplicate subscription,
                    # failed callback verification, ...) — the status alone
                    # made real failures undiagnosable.
                    body = exc.response.text[:300]
                    logger.warning(
                        "Oura webhook subscribe failed for %s/%s: HTTP %d — %s",
                        event_type,
                        data_type,
                        exc.response.status_code,
                        body,
                    )
                    failed.append((event_type, data_type, f"HTTP {exc.response.status_code}: {body}"))
                except Exception as exc:
                    logger.warning("Oura webhook subscribe failed for %s/%s: %s", event_type, data_type, exc)
                    failed.append((event_type, data_type, str(exc)))
    if not created:
        raise RuntimeError(f"All Oura webhook subscriptions failed: {failed[:3]}")
    return {"created": created, "failed": failed}


def _subscription_points_at(subscription: dict, base_url: str) -> bool:
    """True if an Oura subscription's callback targets this deployment's
    webhook endpoint (legacy path or any per-user id)."""
    callback = str(subscription.get("callback_url", ""))
    return callback.startswith(f"{base_url.rstrip('/')}/api/oura/webhook")


async def delete_stale_subscriptions(client_id: str, client_secret: str, base_url: str) -> int:
    """Remove ALL of this app's subscriptions pointing at this deployment.

    Enabling must be a reconcile, not a blind create: Oura rejects duplicate
    (event_type, data_type) subscriptions with 400, so leftovers from the
    legacy endpoint or an earlier webhook id otherwise brick re-enabling
    forever. Foreign callbacks (other deployments on the same OAuth app) are
    logged but left alone. Returns how many were deleted.
    """
    subscriptions = await list_webhook_subscriptions(client_id, client_secret)
    if not isinstance(subscriptions, list):
        return 0
    removed = 0
    for subscription in subscriptions:
        if _subscription_points_at(subscription, base_url):
            await delete_webhook_subscription(client_id, client_secret, str(subscription["id"]))
            removed += 1
        else:
            logger.info(
                "Oura subscription %s points elsewhere (%s) — leaving it",
                subscription.get("id"),
                subscription.get("callback_url"),
            )
    return removed


async def delete_webhook_subscription(client_id: str, client_secret: str, subscription_id: str) -> None:
    """Delete a webhook subscription from Oura API v2."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{OURA_WEBHOOK_URL}/{subscription_id}",
            headers=_webhook_auth_headers(client_id, client_secret),
        )
        resp.raise_for_status()


async def list_webhook_subscriptions(client_id: str, client_secret: str) -> list[dict]:
    """List all active webhook subscriptions for this OAuth app."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            OURA_WEBHOOK_URL,
            headers=_webhook_auth_headers(client_id, client_secret),
        )
        resp.raise_for_status()
        return resp.json()
