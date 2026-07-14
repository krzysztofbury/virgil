"""Partial Oura API failures must not overwrite stored metrics with NULLs."""

import asyncio

import pytest

from app.services.oura_api import (
    DAILY_ENDPOINT_ORDER,
    ENDPOINT_COLUMNS,
    _daily_upsert_sql,
    _upsert_daily,
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
