"""Life score assessments over REST and MCP: only the supplied fields change, and
a row that older forms filled with metrics keeps them (#196)."""

import sqlite3

import pytest
from conftest import user_db_path
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from app.db import LIFE_AREAS
from app.main import app
from app.services.life_scores import LifeScoreError, upsert_life_scores
from app.user_db import close_user_db, open_user_db
from mcp_server import virgil_mcp

DAY = "2026-05-10"
HEADERS = {"X-API-Key": "test-key-123"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def api(auth_client):
    conn = sqlite3.connect(user_db_path())
    saved = conn.execute("SELECT * FROM life_scores").fetchall()
    conn.execute("DELETE FROM life_scores")
    conn.commit()
    yield auth_client, conn
    conn.execute("DELETE FROM life_scores")
    conn.executemany(f"INSERT INTO life_scores VALUES ({', '.join('?' * len(saved[0]))})", saved) if saved else None
    conn.commit()
    conn.close()


def test_a_write_changes_only_the_fields_it_names(api):
    client, conn = api
    first = client.put(f"/api/life-scores/{DAY}", json={"health": 7, "power_level": 7.5}, headers=HEADERS)
    assert first.status_code == 200
    second = client.put(f"/api/life-scores/{DAY}", json={"family": 4, "diagnostic": "Sleep first."}, headers=HEADERS)
    assert second.status_code == 200
    row = second.json()
    assert (row["health"], row["family"], row["power_level"]) == (7, 4, 7.5)
    assert row["diagnostic"] == "Sleep first."


def test_null_clears_a_field(api):
    client, _conn = api
    client.put(f"/api/life-scores/{DAY}", json={"health": 7, "diagnostic": "x", "family": 3}, headers=HEADERS)
    row = client.put(f"/api/life-scores/{DAY}", json={"health": None, "diagnostic": None}, headers=HEADERS).json()
    assert row["health"] is None and row["diagnostic"] == "" and row["family"] == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"health": 0},
        {"health": 11},
        {"health": 5.5},
        {"health": True},
        {"power_level": 10.5},
        {"power_level": -1},
        {"power_level": True},
        {"power_level": "7.5"},
    ],
)
def test_values_outside_the_scale_are_refused(api, payload):
    client, conn = api
    assert client.put(f"/api/life-scores/{DAY}", json=payload, headers=HEADERS).status_code == 422
    assert conn.execute("SELECT COUNT(*) FROM life_scores WHERE date = ?", (DAY,)).fetchone()[0] == 0


def test_an_empty_write_and_an_unknown_field_are_refused(api):
    client, _conn = api
    assert client.put(f"/api/life-scores/{DAY}", json={}, headers=HEADERS).status_code == 422
    assert client.put(f"/api/life-scores/{DAY}", json={"mood": 5}, headers=HEADERS).status_code == 422
    assert client.put("/api/life-scores/2026-13-40", json={"health": 5}, headers=HEADERS).status_code == 422


def test_the_service_refuses_a_column_it_does_not_own(api):
    """The column names go into the SQL, so this is the check that protects it."""
    client, _conn = api

    async def write(db):
        await upsert_life_scores(db, DAY, {"health": 5, "weight = 1; --": 2})

    async def attempt():
        db = await open_user_db(user_db_path().name)
        try:
            await write(db)
        finally:
            await close_user_db(db)

    with pytest.raises(LifeScoreError):
        client.portal.call(attempt)


@pytest.mark.parametrize(
    ("first", "second"),
    [({"health": 5}, {"health": None}), ({"diagnostic": "x"}, {"diagnostic": ""}), ({"health": None}, None)],
    ids=["last score cleared", "last note cleared", "nothing written"],
)
def test_a_write_that_leaves_nothing_leaves_no_row(api, first, second):
    client, conn = api
    last = client.put(f"/api/life-scores/{DAY}", json=first, headers=HEADERS)
    if second is not None:
        last = client.put(f"/api/life-scores/{DAY}", json=second, headers=HEADERS)
    assert last.status_code == 200 and last.json()["health"] is None
    assert conn.execute("SELECT COUNT(*) FROM life_scores WHERE date = ?", (DAY,)).fetchone()[0] == 0


def test_clearing_keeps_a_row_with_older_metrics(api):
    client, conn = api
    conn.execute("INSERT INTO life_scores (date, health, weight) VALUES (?, 6, 80.5)", (DAY,))
    conn.commit()
    client.put(f"/api/life-scores/{DAY}", json={"health": None}, headers=HEADERS)
    assert conn.execute("SELECT weight FROM life_scores WHERE date = ?", (DAY,)).fetchone() == (80.5,)


def test_a_form_line_break_counts_once_toward_the_limit(api):
    client, _conn = api
    text = ("x" * 99 + "\r\n") * 20  # 2020 characters as a browser submits it, 2000 as stored
    row = client.put(f"/api/life-scores/{DAY}", json={"diagnostic": text}, headers=HEADERS)
    assert row.status_code == 200 and "\r" not in row.json()["diagnostic"]


def test_reading_lists_assessments_newest_first_and_skips_bare_metrics(api):
    client, conn = api
    conn.execute("INSERT INTO life_scores (date, weight) VALUES ('2026-05-01', 80.5)")
    conn.commit()
    client.put("/api/life-scores/2026-05-02", json={"power_level": 6.0}, headers=HEADERS)
    client.put("/api/life-scores/2026-05-03", json={"health": 8}, headers=HEADERS)
    body = client.get("/api/life-scores", params={"range": 3650}, headers=HEADERS).json()
    assert [row["date"] for row in body["assessments"]] == ["2026-05-03", "2026-05-02"]
    assert set(body["assessments"][0]) == {"date", *LIFE_AREAS, "power_level", "diagnostic", "priorities"}


def test_a_restored_row_with_null_text_is_handled_like_an_empty_one(api):
    client, conn = api
    conn.execute("INSERT INTO life_scores (date, diagnostic, priorities) VALUES (?, NULL, NULL)", (DAY,))
    conn.commit()
    assert client.delete(f"/api/life-scores/{DAY}", headers=HEADERS).status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM life_scores WHERE date = ?", (DAY,)).fetchone()[0] == 0


def test_deleting_keeps_metrics_older_forms_stored_on_the_row(api):
    client, conn = api
    conn.execute("INSERT INTO life_scores (date, health, weight, pmo_status) VALUES (?, 6, 80.5, 'clean')", (DAY,))
    conn.execute("INSERT INTO life_scores (date, health) VALUES ('2026-05-11', 6)")
    conn.commit()
    assert client.delete(f"/api/life-scores/{DAY}", headers=HEADERS).json() == {"deleted": True, "date": DAY}
    assert conn.execute("SELECT health, weight, pmo_status FROM life_scores WHERE date = ?", (DAY,)).fetchone() == (
        None,
        80.5,
        "clean",
    )
    assert client.delete("/api/life-scores/2026-05-11", headers=HEADERS).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM life_scores WHERE date = '2026-05-11'").fetchone()[0] == 0
    assert client.delete(f"/api/life-scores/{DAY}", headers=HEADERS).status_code == 404, "no assessment left there"


def test_an_agent_records_an_assessment_through_mcp(api, client, monkeypatch):
    rest = TestClient(app, headers=HEADERS)
    rest.portal = client.portal
    monkeypatch.setattr(virgil_mcp, "_client", rest)

    def call(name: str, **arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return client.post("/mcp", headers={**MCP_HEADERS, **HEADERS}, json=body)

    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    }
    client.post("/mcp", headers={**MCP_HEADERS, **HEADERS}, json=body)

    first = call("log_life_scores", date=DAY, health=7, power_level=7.0)
    assert first.json()["result"]["isError"] is False, first.text
    second = call("log_life_scores", date=DAY, priorities="Sleep, then the talk.")
    assert second.json()["result"]["isError"] is False, second.text
    empty = call("log_life_scores", date=DAY)
    assert empty.json()["result"]["isError"] is True, "a call with nothing to record is refused"

    (row,) = api[0].get("/api/life-scores", params={"range": 3650}, headers=HEADERS).json()["assessments"]
    assert (row["health"], row["power_level"], row["priorities"]) == (7, 7.0, "Sleep, then the talk.")


@pytest.mark.parametrize(
    "call",
    [
        lambda day: virgil_mcp.log_life_scores(day, health=5),
        lambda day: virgil_mcp.delete_life_scores(day),
        lambda day: virgil_mcp.log_measurements(day, weight=80),
        lambda day: virgil_mcp.delete_measurements(day),
        lambda day: virgil_mcp.log_daily(day, energy=3),
        lambda day: virgil_mcp.get_daily(day),
        lambda day: virgil_mcp.delete_daily(day),
        lambda day: virgil_mcp.log_freedom_day("porn", day, slipped=False),
        lambda day: virgil_mcp.delete_freedom_day("porn", day),
        lambda day: virgil_mcp.record_balances(day, [{"account": "Cash", "amount": 1}]),
    ],
)
@pytest.mark.parametrize("day", ["../goals/1", "2026-5-10", "May 10", "2026/05/10"])
def test_a_tool_refuses_what_is_not_a_date_before_any_request(monkeypatch, call, day):
    def no_request(*_args, **_kwargs):
        raise AssertionError("a request was made")

    for helper in ("_get", "_put", "_post", "_delete"):
        monkeypatch.setattr(virgil_mcp, helper, no_request, raising=False)
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        call(day)
