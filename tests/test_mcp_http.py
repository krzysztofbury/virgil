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
