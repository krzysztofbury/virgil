import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from urllib.parse import urlencode

import httpx

from app.formatting import time_ago
from app.services.encryption import decrypt, encrypt
from app.services.experiment_weeks import experiment_calendar

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


class OuraReconnectRequired(Exception):
    """The token endpoint refused the refresh token or the client: only a new OAuth login helps."""


# OAuth errors that no retry can fix. Anything else (a timeout, a 5xx, a 429) is
# transient and must not disconnect the integration.
_RECONNECT_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


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
        if resp.status_code in (400, 401):
            try:
                error = resp.json().get("error", "")
            except ValueError:
                error = ""
            if error in _RECONNECT_ERRORS:
                raise OuraReconnectRequired(error)
        resp.raise_for_status()
        return resp.json()


# A refresh token is single-use. The scheduler, a webhook job and the settings page
# can all find the access token expired at once; one refresh per database at a
# time, and the others pick up the token it stored. One process serves every
# request and job, so an in-process lock is enough.
_refresh_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _oura_integration(db) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
    return dict(rows[0]) if rows else None


def _needs_refresh(integration: dict, now: datetime) -> bool:
    expires_at = integration.get("token_expires_at", "")
    if not expires_at:
        return False
    try:
        exp_dt = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        # Corrupted expiry timestamp: we cannot tell whether the token is still
        # valid, so use it as-is and let the API answer.
        logger.warning("Could not parse token_expires_at=%r, using token as-is", expires_at)
        return False
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=UTC)
    return now >= exp_dt - timedelta(minutes=5)


# Oura publishes a day's sleep and readiness once, in the morning; a sync older
# than a day can be missing today's.
OURA_STALE_AFTER = timedelta(hours=24)


async def oura_sync_freshness(db, now: datetime | None = None) -> dict | None:
    """When Oura data last arrived, for an account with Oura connected; None otherwise.

    `age` is None when it never synced, which the page states rather than hides.
    """
    integration = await _oura_integration(db)
    if not integration or integration["status"] != "connected":
        return None
    now = now or datetime.now(UTC)
    try:
        synced = datetime.fromisoformat(integration["last_sync_at"])
    except (TypeError, ValueError):
        return {"age": None, "local": None, "stale": True}
    if synced.tzinfo is None:
        synced = synced.replace(tzinfo=UTC)
    return {
        "age": time_ago(synced, now),
        "local": synced.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "stale": now - synced > OURA_STALE_AFTER,
    }


async def ensure_valid_token(db) -> str | None:
    """Check token expiry, auto-refresh if needed, return decrypted access_token or None."""
    from app.services.backup import db_main_path

    integration = await _oura_integration(db)
    if not integration or integration["status"] != "connected" or not decrypt(integration["access_token_enc"]):
        return None
    if not _needs_refresh(integration, datetime.now(UTC)):
        return decrypt(integration["access_token_enc"])

    async with _refresh_locks[await db_main_path(db)]:
        # Another caller may have refreshed while this one waited for the lock.
        integration = await _oura_integration(db)
        if not integration or integration["status"] != "connected":
            return None
        now = datetime.now(UTC)
        if not _needs_refresh(integration, now):
            return decrypt(integration["access_token_enc"]) or None
        return await _refresh(db, integration, now)


async def _refresh(db, integration: dict, now: datetime) -> str | None:
    refresh_tok = decrypt(integration["refresh_token_enc"])
    if not refresh_tok:
        return None
    try:
        tokens = await refresh_access_token(
            integration["client_id"], decrypt(integration["client_secret_enc"]), refresh_tok
        )
        new_access = tokens["access_token"]
    except OuraReconnectRequired as error:
        logger.warning("Oura refused the refresh token (%s); the integration needs reconnecting", error)
        await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
        await db.commit()
        return None
    except Exception:
        # Still connected: the next sync tries again with the same refresh token.
        logger.warning("Oura token refresh failed; will retry on the next sync", exc_info=True)
        return None
    new_refresh = tokens.get("refresh_token", refresh_tok)
    new_expires = (now + timedelta(seconds=tokens.get("expires_in", 86400))).isoformat()
    await db.execute(
        """UPDATE integrations SET access_token_enc = ?, refresh_token_enc = ?,
           token_expires_at = ? WHERE provider = 'oura'""",
        (encrypt(new_access), encrypt(new_refresh), new_expires),
    )
    await db.commit()
    return new_access


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
MONTHLY_ENDPOINT_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily_sleep": ("sleep_score",),
    "daily_readiness": ("readiness",),
    "daily_activity": ("activity", "steps"),
    "daily_stress": ("stress_normal", "stress_stressful", "stress_restored"),
    "sleep": ("sleep_duration", "deep_sleep", "rem_sleep", "lowest_hr", "hrv"),
    "heartrate": ("rhr",),
}
_ALL_MONTHLY_COLUMNS = tuple(
    column for endpoint in DAILY_ENDPOINT_ORDER for column in MONTHLY_ENDPOINT_COLUMNS[endpoint]
)


@dataclass(frozen=True)
class OuraSyncResult:
    days: int
    failed_daily_endpoints: tuple[str, ...]
    workouts_synced: bool

    @property
    def complete(self) -> bool:
        return not self.failed_daily_endpoints and self.workouts_synced


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


# A page cap, so a provider that keeps answering with a next_token cannot keep
# a sync running forever.
OURA_PAGES_MAX = 50
# Heart rate comes as samples every few minutes, and Oura bounds the range one
# request may cover; a week per request stays well inside it.
HEARTRATE_WINDOW_DAYS = 7


def _local_midnight(day: str) -> str:
    """The start of a day in the server's time zone, as Oura's date-time parameters take it."""
    return datetime.combine(date.fromisoformat(day), datetime.min.time()).astimezone().isoformat()


def _range_params(endpoint: str, start: str, end: str) -> dict[str, str]:
    """Query parameters for days start up to, not including, end.

    The daily endpoints take dates; heartrate takes date-times and ignores
    start_date and end_date, which left it on its default window and resting_hr
    empty.
    """
    if endpoint == "heartrate":
        return {"start_datetime": _local_midnight(start), "end_datetime": _local_midnight(end)}
    return {"start_date": start, "end_date": end}


async def _fetch_endpoint(
    client: httpx.AsyncClient, endpoint: str, token: str, start: str, end: str, max_retries: int = 3
) -> list:
    """Every item for the range, following next_token across pages."""
    items: list = []
    params = _range_params(endpoint, start, end)
    for _page in range(OURA_PAGES_MAX):
        payload = await _fetch_page(client, endpoint, token, params, max_retries)
        items.extend(payload.get("data", []))
        next_token = payload.get("next_token")
        if not next_token:
            return items
        params = {**_range_params(endpoint, start, end), "next_token": next_token}
    raise OuraFetchError(f"Oura API {endpoint} returned more than {OURA_PAGES_MAX} pages")


async def _fetch_page(
    client: httpx.AsyncClient, endpoint: str, token: str, params: dict[str, str], max_retries: int
) -> dict:
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(
                f"{OURA_API_BASE}/{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        except httpx.RequestError as exc:
            raise OuraFetchError(f"Oura API {endpoint} transport failed") from exc
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError as exc:
                raise OuraFetchError(f"Oura API {endpoint} returned malformed JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("data", []), list):
                raise OuraFetchError(f"Oura API {endpoint} returned an unexpected response")
            return payload
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


def _local_day(timestamp: object) -> str:
    """The server-local day of a sample. Oura stamps heart rate in UTC, so its
    first ten characters put the hours after local midnight on the day before."""
    if not isinstance(timestamp, str):
        return ""
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone().date().isoformat()


async def _fetch_heartrate(client: httpx.AsyncClient, token: str, start: str, end: str, ok_endpoints: set[str]) -> list:
    """Heart rate samples for the range, a week per request. All weeks or none:
    a partial range would write resting_hr from a fraction of some days."""
    samples: list = []
    day, last = date.fromisoformat(start), date.fromisoformat(end)
    try:
        while day < last:
            until = min(day + timedelta(days=HEARTRATE_WINDOW_DAYS), last)
            samples.extend(await _fetch_endpoint(client, "heartrate", token, day.isoformat(), until.isoformat()))
            day = until
    except OuraFetchError:
        return []
    ok_endpoints.add("heartrate")
    return samples


async def fetch_oura_daily(access_token: str, start_date: str, end_date: str) -> tuple[dict[str, dict], set[str]]:
    """Fetch multiple Oura endpoints and merge into per-day dicts.

    Returns (daily_data, ok_endpoints). Endpoints missing from ok_endpoints
    failed and their columns must not be written over existing data. end_date
    is exclusive, as Oura treats it.
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
        hr_data = await _fetch_heartrate(client, access_token, start_date, end_date, ok_endpoints)
        hr_by_day: dict[str, list] = defaultdict(list)
        for item in hr_data:
            d = _local_day(item.get("timestamp"))
            bpm = item.get("bpm")
            if d and bpm:
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
        _, end = experiment_calendar(start, exp["num_weeks"])

        # Get duration metrics with source_match configured — Oura workouts are
        # minutes, so only duration metrics may auto-import.
        type_rows = await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types "
            "WHERE experiment_id = ? AND source_match != '' AND kind = 'duration'",
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
            "SELECT * FROM oura_workouts WHERE date >= ? AND date <= ?",
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
                "(experiment_id, date, activity_type_id, value, notes, source, source_ref) "
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


async def sync_oura_from_api(db, days_back: int = 30) -> OuraSyncResult:
    """Full sync pipeline: ensure token → fetch → upsert oura_daily → recompute oura_monthly.

    Partial endpoint failures update only the successfully fetched columns;
    a total failure raises instead of committing destructive NULLs.
    """
    token = await ensure_valid_token(db)
    if not token:
        raise RuntimeError("No valid Oura token available")

    # Oura leaves the end_date day out, so the range ends tomorrow: ending it
    # today never fetched today's activity and steps.
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days_back + 1)
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

    workouts_synced = True
    workouts = []
    try:
        workouts = await fetch_oura_workouts(token, start.isoformat(), end.isoformat())
    except OuraAuthError:
        logger.warning("Oura token rejected during workout sync - marking integration as error")
        await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
        await db.commit()
        raise
    except OuraFetchError:
        logger.warning("Oura workout endpoint unavailable or not authorized - skipping workout sync")
        workouts_synced = False
    except Exception:
        logger.exception("Failed to fetch Oura workouts")
        workouts_synced = False

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

        def avg(field, _days=days):
            vals = [d[field] for d in _days if d.get(field) is not None]
            return round(sum(vals) / len(vals), 1) if vals else None

        def total(field, _days=days):
            vals = [d[field] for d in _days if d.get(field) is not None]
            return sum(vals) if vals else None

        # Oura API v2 daily_stress currently sends only stress_high and
        # recovery_high, so stress_low (and stress_normal) stays 0 until it does.
        steps_average = avg("steps")
        monthly_values = {
            "sleep_score": avg("sleep_score"),
            "readiness": avg("readiness_score"),
            "activity": avg("activity_score"),
            "steps": round(steps_average) if steps_average is not None else None,
            "sleep_duration": avg("sleep_duration_hours"),
            "deep_sleep": avg("deep_sleep_hours"),
            "rem_sleep": avg("rem_sleep_hours"),
            "rhr": avg("resting_hr"),
            "lowest_hr": avg("lowest_hr"),
            "hrv": avg("avg_hrv"),
            "stress_normal": total("stress_low"),
            "stress_stressful": total("stress_high"),
            "stress_restored": total("stress_rest"),
        }
        update_columns = tuple(
            column
            for endpoint in DAILY_ENDPOINT_ORDER
            if endpoint in ok_endpoints
            for column in MONTHLY_ENDPOINT_COLUMNS[endpoint]
        )
        assert update_columns, "Monthly aggregation requires at least one successful endpoint"
        insert_columns = ", ".join(("month", *_ALL_MONTHLY_COLUMNS))
        placeholders = ", ".join("?" for _ in range(len(_ALL_MONTHLY_COLUMNS) + 1))
        update_clause = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
        await db.execute(
            f"INSERT INTO oura_monthly ({insert_columns}) VALUES ({placeholders}) "
            f"ON CONFLICT(month) DO UPDATE SET {update_clause}",
            (month, *(monthly_values[column] for column in _ALL_MONTHLY_COLUMNS)),
        )

    # Every external fetch is complete before the first domain write, so job
    # heartbeats never observe a SQLite transaction held across network I/O.
    for workout in workouts:
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
                workout["date"],
                workout["activity"],
                workout["duration_minutes"],
                workout["calories"],
                workout["distance_meters"],
                workout["intensity"],
                workout["start_datetime"],
                workout["end_datetime"],
                workout["oura_id"],
            ),
        )
    await _auto_populate_experiments(db)

    # Update last_sync_at
    now = datetime.now(UTC).isoformat()
    await db.execute("UPDATE integrations SET last_sync_at = ? WHERE provider = 'oura'", (now,))
    await db.commit()
    return OuraSyncResult(
        days=count,
        failed_daily_endpoints=tuple(sorted(failed)),
        workouts_synced=workouts_synced,
    )


# ── Webhook Subscription API ──


def _webhook_auth_headers(client_id: str, client_secret: str) -> dict[str, str]:
    """Oura's /v2/webhook/subscription endpoints authenticate with the OAuth
    APP credentials (x-client-id / x-client-secret headers), NOT a user Bearer
    token — per the ClientIdAuth/ClientSecretAuth security schemes in the
    Oura OpenAPI spec."""
    return {"x-client-id": client_id, "x-client-secret": client_secret}


async def create_oura_webhook_subscription(
    client_id: str,
    client_secret: str,
    callback_url: str,
    verification_token: str,
    event_type: str,
    data_type: str,
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            OURA_WEBHOOK_URL,
            headers=_webhook_auth_headers(client_id, client_secret),
            json={
                "callback_url": callback_url,
                "verification_token": verification_token,
                "event_type": event_type,
                "data_type": data_type,
            },
        )
        response.raise_for_status()
        return response.json()


async def renew_oura_webhook_subscription(client_id: str, client_secret: str, subscription_id: str) -> dict:
    """Renew an Oura subscription through the documented renewal route."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(
            f"{OURA_WEBHOOK_URL}/renew/{subscription_id}",
            headers=_webhook_auth_headers(client_id, client_secret),
        )
        response.raise_for_status()
        return response.json()


async def delete_oura_webhook_subscription(client_id: str, client_secret: str, subscription_id: str) -> None:
    """Delete a webhook subscription from Oura API v2."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.delete(
            f"{OURA_WEBHOOK_URL}/{subscription_id}",
            headers=_webhook_auth_headers(client_id, client_secret),
        )
        resp.raise_for_status()


async def list_oura_webhook_subscriptions(client_id: str, client_secret: str) -> list[dict]:
    """List all active webhook subscriptions for this OAuth app."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            OURA_WEBHOOK_URL,
            headers=_webhook_auth_headers(client_id, client_secret),
        )
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, list):
            raise RuntimeError("Oura subscription list response is not an array")
        return payload
