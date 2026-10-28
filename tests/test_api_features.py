"""Feature flags over REST and MCP (#177): only the modules Settings offers, each behind its data's scope."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, user_db_path
from fastapi.testclient import TestClient

from app.main import app
from mcp_server import virgil_mcp

KEY = {"X-API-Key": "test-key-123"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _token(scopes: list[str]) -> dict:
    from app.central_db import create_api_token

    central = sqlite3.connect(Path(_TMP) / "central.db")
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.close()
    return {"Authorization": f"Bearer {asyncio.run(create_api_token(user_id, 'test', scopes, 90))}"}


def _stored(key: str) -> str | None:
    with sqlite3.connect(user_db_path()) as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


@pytest.fixture(autouse=True)
def _flags_and_tokens_restored(auth_client):
    before = {key: _stored(key) for key in ("feature_freedom", "feature_finance")}
    yield
    with sqlite3.connect(user_db_path()) as conn:
        for key, value in before.items():
            if value is None:
                conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            else:
                conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", (value, key))
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute("DELETE FROM api_tokens")
    central.commit()
    central.close()


def test_a_known_flag_set_over_the_api_switches_the_module_in_the_app(auth_client):
    token = _token(["read", "write", "finance"])
    off = auth_client.put("/api/features/finance", headers=token, json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json() == {"name": "finance", "enabled": False}
    assert auth_client.get("/finance", follow_redirects=False).status_code == 303

    on = auth_client.put("/api/features/finance", headers=token, json={"enabled": True})
    assert on.json() == {"name": "finance", "enabled": True}
    assert auth_client.get("/api/features", headers=token).json()["features"]["finance"] is True
    assert auth_client.get("/finance", follow_redirects=False).status_code == 200


@pytest.mark.parametrize("name", ["admin", "feniks", "no_porn", "FINANCE", "freedom_on_dashboard"])
def test_an_unknown_flag_is_refused_and_not_stored(auth_client, name):
    response = auth_client.put(
        f"/api/features/{name}",
        headers=_token(["read", "write", "sensitive", "finance"]),
        json={"enabled": True},
    )
    assert response.status_code == 404
    assert "finance" in response.json()["detail"] and "freedom" in response.json()["detail"]
    assert _stored(f"feature_{name}") is None


@pytest.mark.parametrize(
    ("scopes", "name", "missing"),
    [
        (["read"], "finance", "write"),
        (["read", "write"], "freedom", "sensitive"),
        (["read", "write", "finance"], "freedom", "sensitive"),
        (["read", "write", "sensitive"], "finance", "finance"),
    ],
)
def test_a_token_without_the_flags_scope_cannot_set_it(auth_client, scopes, name, missing):
    before = _stored(f"feature_{name}")
    response = auth_client.put(f"/api/features/{name}", headers=_token(scopes), json={"enabled": before != "1"})
    assert response.status_code == 403
    assert f"lacks the {missing} scope" in response.json()["detail"]
    assert _stored(f"feature_{name}") == before


def test_the_server_key_without_sensitive_cannot_switch_freedom(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", False)
    response = auth_client.put("/api/features/freedom", headers=KEY, json={"enabled": True})
    assert response.status_code == 403
    assert "VIRGIL_API_SENSITIVE" in response.json()["detail"]


def test_reading_shows_a_flag_only_with_its_scope(auth_client):
    plain = auth_client.get("/api/features", headers=_token(["read"]))
    assert plain.status_code == 200
    assert plain.json()["features"] == {"freedom": None, "finance": None}

    full = auth_client.get("/api/features", headers=_token(["read", "sensitive", "finance"])).json()["features"]
    assert set(full) == {"freedom", "finance"}
    assert all(isinstance(value, bool) for value in full.values())


def _rpc(client: TestClient, headers: dict, name: str, arguments: dict):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    return client.post("/mcp", headers={**MCP_HEADERS, **headers}, json=body).json()["result"]


@pytest.fixture
def hosted_tools(client, monkeypatch):
    """Tool calls loop back to the in-process REST API with the caller's own credential."""
    import app.mcp_endpoint as endpoint

    rest = TestClient(app)
    rest.portal = client.portal
    rest.event_hooks = {"request": [endpoint._forward_caller_secret]}
    monkeypatch.setattr(virgil_mcp, "_client", rest)


def test_mcp_feature_tools_keep_the_allowlist_and_the_scopes(auth_client, client, hosted_tools):
    writer = _token(["read", "write", "finance"])
    result = _rpc(client, writer, "set_feature", {"name": "finance", "enabled": True})
    assert result["isError"] is False, result
    features = _rpc(client, writer, "get_features", {})
    assert json.loads(features["content"][0]["text"])["features"] == {"freedom": None, "finance": True}

    unknown = _rpc(client, writer, "set_feature", {"name": "admin", "enabled": True})
    assert unknown["isError"] is True and "404" in unknown["content"][0]["text"]

    reader = _token(["read", "finance"])
    refused = _rpc(client, reader, "set_feature", {"name": "finance", "enabled": False})
    assert refused["isError"] is True and "lacks the write scope" in refused["content"][0]["text"]
    assert _stored("feature_finance") == "1"


@pytest.mark.parametrize("name", ["", "fin.ance", "a b", "freedom/x"])
def test_mcp_refuses_a_malformed_flag_name_with_the_known_ones(auth_client, client, hosted_tools, name):
    """Such a name never reaches the API's 404, so the tool has to say it plainly."""
    writer = _token(["read", "write", "sensitive", "finance"])
    refused = _rpc(client, writer, "set_feature", {"name": name, "enabled": True})
    assert refused["isError"] is True
    assert "freedom" in refused["content"][0]["text"] and "finance" in refused["content"][0]["text"]


def test_get_health_reports_an_unreachable_host_as_data(monkeypatch):
    """The moment health matters most is when the host does not answer."""
    import httpx

    monkeypatch.setattr(virgil_mcp, "_client", httpx.Client(base_url="http://127.0.0.1:9", timeout=2.0))
    report = virgil_mcp.get_health()
    for probe in ("livez", "healthz"):
        assert report[probe]["status_code"] is None
        assert "ConnectError" in report[probe]["error"]
