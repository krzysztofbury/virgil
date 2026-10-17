"""Hosted MCP endpoint (/mcp): the API-key gate and a real tool call over Streamable HTTP."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from mcp_server import virgil_mcp

KEY = {"X-API-Key": "test-key-123"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client: TestClient, method: str, params: dict | None = None, headers: dict = KEY):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers={**MCP_HEADERS, **headers}, json=body)


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}], ids=["missing", "wrong"])
def test_mcp_refuses_request_without_valid_key(client, headers):
    resp = _rpc(client, "tools/list", headers=headers)
    assert resp.status_code == 401
    assert "result" not in resp.text


def test_mcp_tool_write_lands_in_api_user_db(auth_client, client, monkeypatch):
    # Production reaches the REST API over loopback; here the same client seam
    # points at the in-process app, sharing the lifespan's event loop.
    rest = TestClient(app, headers=KEY)
    rest.portal = client.portal
    monkeypatch.setattr(virgil_mcp, "_client", rest)

    init = _rpc(
        client,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    )
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "virgil"

    day = (date.today() - timedelta(days=3)).isoformat()
    call = _rpc(client, "tools/call", {"name": "log_measurements", "arguments": {"date": day, "weight": 81.5}})
    assert call.status_code == 200, call.text
    assert call.json()["result"]["isError"] is False, call.text

    stored = auth_client.get("/api/measurements", headers=KEY).json()["measurements"]
    assert [row["weight"] for row in stored if row["date"] == day] == [81.5]


def test_freedom_tools_write_through_the_new_paths_and_the_old_names_still_work(auth_client, client, monkeypatch):
    from conftest import track_porn

    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    rest = TestClient(app, headers=KEY)
    rest.portal = client.portal
    monkeypatch.setattr(virgil_mcp, "_client", rest)
    track_porn()
    old_day = (date.today() - timedelta(days=61)).isoformat()
    new_day = (date.today() - timedelta(days=62)).isoformat()

    calls = (
        ("log_feniks_day", {"date": old_day, "used": True, "minutes": 9}),
        ("log_freedom_day", {"habit": "porn", "date": new_day, "slipped": False, "note": "ZZ clean"}),
    )
    try:
        for name, arguments in calls:
            call = _rpc(client, "tools/call", {"name": name, "arguments": arguments})
            assert call.status_code == 200, call.text
            assert call.json()["result"]["isError"] is False, call.text

        days = {row["date"]: row for row in auth_client.get("/api/noporn?range=90", headers=KEY).json()["daily"]}
        assert (days[old_day]["used"], days[old_day]["minutes"]) == (1, 9)
        assert (days[new_day]["used"], days[new_day]["note"]) == (0, "ZZ clean")
    finally:
        for day in (old_day, new_day):
            auth_client.delete(f"/api/freedom/porn/days/{day}", headers=KEY)
