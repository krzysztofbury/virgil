"""Partial Oura API failures must not overwrite stored metrics with NULLs."""

import asyncio
from datetime import date

import httpx
import pytest

from app.services.oura_api import (
    DAILY_ENDPOINT_ORDER,
    ENDPOINT_COLUMNS,
    OuraAuthError,
    OuraSyncResult,
    _daily_upsert_sql,
    _fetch_optional,
    _upsert_daily,
    sync_oura_from_api,
)


def test_update_clause_limited_to_successful_endpoints():
    sql = _daily_upsert_sql({"daily_activity"})
    _, update_clause = sql.split("DO UPDATE SET", 1)
    assert "activity_score=excluded.activity_score" in update_clause
    assert "steps=excluded.steps" in update_clause
    assert "sleep_score" not in update_clause
    assert "avg_hrv" not in update_clause


def test_update_clause_covers_all_when_everything_succeeds():
    sql = _daily_upsert_sql(set(DAILY_ENDPOINT_ORDER))
    _, update_clause = sql.split("DO UPDATE SET", 1)
    for endpoint in DAILY_ENDPOINT_ORDER:
        for col in ENDPOINT_COLUMNS[endpoint]:
            assert f"{col}=excluded.{col}" in update_clause


def test_no_successful_endpoints_refused():
    with pytest.raises(AssertionError):
        _daily_upsert_sql(set())


def test_failed_endpoint_columns_preserved_in_db(tmp_path):
    """Existing sleep_score survives a sync where only daily_activity succeeded."""

    async def scenario():
        import aiosqlite

        from app.migrations.runner import run_migrations

        db = await aiosqlite.connect(tmp_path / "partial.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)

        # Day already synced with a sleep score.
        await _upsert_daily(db, "2026-07-01", {"sleep_score": 80, "steps": 1000}, set(DAILY_ENDPOINT_ORDER))
        await db.commit()

        # Next sync: daily_sleep endpoint failed, activity succeeded.
        await _upsert_daily(db, "2026-07-01", {"activity_score": 70, "steps": 5000}, {"daily_activity"})
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date = '2026-07-01'")
        row = dict(rows[0])
        await db.close()
        return row

    row = asyncio.run(scenario())
    assert row["sleep_score"] == 80, "Failed endpoint's column was overwritten"
    assert row["activity_score"] == 70
    assert row["steps"] == 5000


def test_partial_sync_preserves_failed_monthly_columns_and_uses_non_null_step_count(tmp_path, monkeypatch):
    async def scenario():
        import aiosqlite

        from app.migrations.runner import run_migrations

        db = await aiosqlite.connect(tmp_path / "monthly-partial.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        today = date.today().isoformat()
        month = today[:7]
        other_day = f"{month}-02" if today.endswith("-01") else f"{month}-01"
        await db.execute(
            "INSERT INTO oura_daily (date, sleep_score, steps) VALUES (?, 80, 1000)",
            (today,),
        )
        await db.execute("INSERT INTO oura_daily (date, sleep_score) VALUES (?, 70)", (other_day,))
        await db.execute(
            "INSERT INTO oura_monthly (month, sleep_score, activity, steps) VALUES (?, 77, 60, 1000)",
            (month,),
        )
        await db.commit()

        async def fake_token(_db):
            return "token"

        async def fake_daily(_token, _start, _end):
            return {today: {"activity_score": 70, "steps": 5000}}, {"daily_activity"}

        async def fake_workouts(_token, _start, _end):
            return []

        monkeypatch.setattr("app.services.oura_api.ensure_valid_token", fake_token)
        monkeypatch.setattr("app.services.oura_api.fetch_oura_daily", fake_daily)
        monkeypatch.setattr("app.services.oura_api.fetch_oura_workouts", fake_workouts)

        result = await sync_oura_from_api(db, days_back=1)
        monthly = dict((await db.execute_fetchall("SELECT * FROM oura_monthly WHERE month = ?", (month,)))[0])
        daily = dict((await db.execute_fetchall("SELECT * FROM oura_daily WHERE date = ?", (today,)))[0])
        await db.close()
        return result, monthly, daily

    result, monthly, daily = asyncio.run(scenario())
    assert result.failed_daily_endpoints == tuple(sorted(set(DAILY_ENDPOINT_ORDER) - {"daily_activity"}))
    assert monthly["sleep_score"] == 77, "a failed daily_sleep source must not rewrite its monthly aggregate"
    assert monthly["activity"] == 70
    assert monthly["steps"] == 5000, "NULL step rows must not dilute the monthly average"
    assert daily["sleep_score"] == 80


def test_workout_auth_failure_marks_integration_error_and_propagates(tmp_path, monkeypatch):
    async def scenario():
        import aiosqlite

        from app.migrations.runner import run_migrations

        db = await aiosqlite.connect(tmp_path / "workout-auth.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        await db.execute(
            "INSERT INTO integrations (provider, client_id, client_secret_enc, status) "
            "VALUES ('oura', 'client', 'secret', 'connected')"
        )
        await db.commit()

        async def fake_token(_db):
            return "token"

        async def fake_daily(_token, _start, _end):
            return {date.today().isoformat(): {"sleep_score": 80}}, set(DAILY_ENDPOINT_ORDER)

        async def rejected_workouts(_token, _start, _end):
            raise OuraAuthError("revoked")

        monkeypatch.setattr("app.services.oura_api.ensure_valid_token", fake_token)
        monkeypatch.setattr("app.services.oura_api.fetch_oura_daily", fake_daily)
        monkeypatch.setattr("app.services.oura_api.fetch_oura_workouts", rejected_workouts)

        with pytest.raises(OuraAuthError):
            await sync_oura_from_api(db, days_back=1)
        status = (await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'"))[0]["status"]
        await db.close()
        return status

    assert asyncio.run(scenario()) == "error"


@pytest.mark.parametrize("mode", ["transport", "malformed", "wrong-shape"])
def test_optional_fetch_absorbs_normalized_endpoint_failures(mode):
    class FakeClient:
        async def get(self, url, headers, params):
            request = httpx.Request("GET", url)
            if mode == "transport":
                raise httpx.ConnectError("offline", request=request)
            if mode == "malformed":
                return httpx.Response(200, text="not-json", request=request)
            return httpx.Response(200, json={"data": {}}, request=request)

    async def scenario():
        ok_endpoints = set()
        data = await _fetch_optional(FakeClient(), "daily_sleep", "token", "2026-08-01", "2026-08-02", ok_endpoints)
        return data, ok_endpoints

    data, ok_endpoints = asyncio.run(scenario())
    assert data == []
    assert ok_endpoints == set()


def test_scheduled_partial_sync_is_a_completed_attempt(monkeypatch, caplog):
    from app.services.scheduler import _run_oura_sync_task

    async def fake_sync(_db):
        return OuraSyncResult(days=2, failed_daily_endpoints=("sleep",), workouts_synced=False)

    monkeypatch.setattr("app.services.oura_api.sync_oura_from_api", fake_sync)
    with caplog.at_level("WARNING", logger="app.services.scheduler"):
        asyncio.run(_run_oura_sync_task(object()))
    assert "Scheduled Oura sync partial" in caplog.text
