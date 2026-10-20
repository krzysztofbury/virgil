"""Oura data says how fresh it is, so a quiet day is not mistaken for a stalled sync."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.migrations.runner import run_migrations
from app.services.oura_api import oura_sync_freshness
from tests.conftest import user_db_path

_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def _freshness(tmp_path, status: str | None, last_sync_at: str = ""):
    async def scenario():
        db = await aiosqlite.connect(tmp_path / "oura.db")
        db.row_factory = aiosqlite.Row
        try:
            await run_migrations(db)
            if status:
                await db.execute(
                    "INSERT INTO integrations (provider, client_id, client_secret_enc, status, last_sync_at) "
                    "VALUES ('oura', 'id', 'secret', ?, ?)",
                    (status, last_sync_at),
                )
                await db.commit()
            return await oura_sync_freshness(db, now=_NOW)
        finally:
            await db.close()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("synced_ago", "age", "stale"),
    [
        (timedelta(hours=3), "3 hours ago", False),
        (timedelta(hours=24), "1 day ago", False),
        (timedelta(hours=24, minutes=1), "1 day ago", True),
    ],
)
def test_a_sync_older_than_a_day_is_stale(tmp_path, synced_ago, age, stale):
    result = _freshness(tmp_path, "connected", (_NOW - synced_ago).isoformat())

    assert (result["age"], result["stale"]) == (age, stale)


def test_a_connected_account_that_never_synced_says_so(tmp_path):
    assert _freshness(tmp_path, "connected") == {"age": None, "local": None, "stale": True}


@pytest.mark.parametrize("status", [None, "configured", "error"])
def test_nothing_is_said_without_a_connected_oura(tmp_path, status):
    assert _freshness(tmp_path, status, _NOW.isoformat()) is None


@pytest.fixture
def connected_oura():
    conn = sqlite3.connect(user_db_path())
    synced = (datetime.now(UTC) - timedelta(days=2, hours=1)).isoformat()
    conn.execute("DELETE FROM integrations WHERE provider = 'oura'")
    conn.execute(
        "INSERT INTO integrations (provider, client_id, client_secret_enc, status, last_sync_at) "
        "VALUES ('oura', 'id', 'secret', 'connected', ?)",
        (synced,),
    )
    conn.commit()
    try:
        yield
    finally:
        conn.execute("DELETE FROM integrations WHERE provider = 'oura'")
        conn.commit()
        conn.close()


@pytest.mark.parametrize("page", ["/", "/oura"])
def test_the_dashboard_and_oura_page_show_a_stale_sync(auth_client, connected_oura, page):
    html = auth_client.get(page).text

    assert '<small class="oura-freshness is-stale"' in html
    assert "Last synced 2 days ago</small>" in html
