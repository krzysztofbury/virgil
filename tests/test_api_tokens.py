"""Personal API tokens (#94 phase 2): each serves its own account, with its own scopes, on /api and /mcp."""

import asyncio
import sqlite3
import uuid
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, TEST_PASSWORD
from fastapi.testclient import TestClient

from app.main import app
from mcp_server import virgil_mcp

DAY = "2026-07-05"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _central():
    return sqlite3.connect(Path(_TMP) / "central.db")


def _token(email: str, scopes: list[str], days: int | None = 90) -> str:
    from app.central_db import create_api_token

    central = _central()
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
    central.close()
    return asyncio.run(create_api_token(user_id, "test", scopes, days))


def _bearer(secret: str) -> dict:
    return {"Authorization": f"Bearer {secret}"}


@pytest.fixture
def other_account(client):
    """A second account with its own database and its own daily log for DAY."""
    from app.auth import hash_password
    from app.user_db import create_user_db

    user_id = str(uuid.uuid4())
    filename = f"{user_id}.db"
    central = _central()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, 'other@example.com', ?, ?)",
        (user_id, hash_password("other-password-1"), filename),
    )
    central.commit()
    asyncio.run(create_user_db(filename))
    path = Path(_TMP, "users", filename)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO daily_logs (date, energy) VALUES (?, 2)", (DAY,))
    conn.commit()
    conn.close()
    try:
        yield "other@example.com"
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


@pytest.fixture
def own_day(auth_client):
    # By name, not user_db_path(): other_account may already have added a second file.
    central = _central()
    filename = central.execute("SELECT db_filename FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.close()
    conn = sqlite3.connect(Path(_TMP, "users", filename))
    conn.execute("INSERT OR REPLACE INTO daily_logs (date, energy) VALUES (?, 9)", (DAY,))
    conn.commit()
    try:
        yield
    finally:
        conn.execute("DELETE FROM daily_logs WHERE date = ?", (DAY,))
        conn.commit()
        conn.close()


@pytest.fixture(autouse=True)
def _no_tokens_left_behind():
    yield
    central = _central()
    central.execute("DELETE FROM api_tokens")
    central.commit()
    central.close()


def test_a_token_serves_its_own_account_and_no_other(auth_client, other_account, own_day):
    mine = _token(TEST_EMAIL, ["read"])
    theirs = _token(other_account, ["read"])
    central = _central()
    stored = [row[0] for row in central.execute("SELECT token_hash FROM api_tokens")]
    central.close()
    assert mine not in stored and theirs not in stored, "only a hash is stored"

    assert auth_client.get(f"/api/daily/{DAY}", headers=_bearer(mine)).json()["energy"] == 9
    assert auth_client.get(f"/api/daily/{DAY}", headers=_bearer(theirs)).json()["energy"] == 2
    assert auth_client.get(f"/api/daily/{DAY}", headers={"X-API-Key": theirs}).json()["energy"] == 2


def test_two_different_credentials_in_one_request_are_refused(auth_client):
    mine = _token(TEST_EMAIL, ["read"])
    response = auth_client.get("/api/summary", headers={"X-API-Key": "test-key-123", **_bearer(mine)})
    assert response.status_code == 401


@pytest.mark.parametrize("how", ["revoked", "expired", "account disabled"])
def test_a_dead_token_is_refused_not_rerouted(auth_client, other_account, how):
    secret = _token(other_account, ["read"])
    central = _central()
    if how == "revoked":
        central.execute("UPDATE api_tokens SET revoked_at = '2026-01-01T00:00:00Z'")
    elif how == "expired":
        central.execute("UPDATE api_tokens SET expires_at = '2020-01-01T00:00:00Z'")
    else:
        central.execute("UPDATE users SET is_active = 0 WHERE email = ?", (other_account,))
    central.commit()
    central.close()
    assert auth_client.get(f"/api/daily/{DAY}", headers=_bearer(secret)).status_code == 401


def test_use_is_recorded_once_a_minute(auth_client):
    secret = _token(TEST_EMAIL, ["read"])
    central = _central()
    for _ in range(3):
        auth_client.get("/api/summary", headers=_bearer(secret))
    last_used = central.execute("SELECT last_used_at FROM api_tokens").fetchone()[0]
    central.close()
    assert last_used


def test_without_the_server_key_the_api_is_off_until_a_token_exists(auth_client, monkeypatch):
    import app.routers.api as api_module

    monkeypatch.setattr(api_module, "API_KEY", "")
    off = auth_client.get("/api/summary")
    assert off.status_code == 403 and "API disabled" in off.json()["detail"]

    secret = _token(TEST_EMAIL, ["read"])
    assert auth_client.get("/api/summary").status_code == 401
    assert auth_client.get("/api/summary", headers={"X-API-Key": "test-key-123"}).status_code == 401
    assert auth_client.get("/api/summary", headers=_bearer(secret)).status_code == 200


def test_the_summary_hides_freedom_from_a_token_without_the_sensitive_scope(auth_client):
    plain = _token(TEST_EMAIL, ["read"])
    sensitive = _token(TEST_EMAIL, ["read", "sensitive"])
    assert auth_client.get("/api/summary", headers=_bearer(plain)).json()["feniks"] is None
    assert auth_client.get("/api/summary", headers=_bearer(sensitive)).json()["feniks"] is not None


def _rpc(client: TestClient, secret: str, method: str, params: dict | None = None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post("/mcp", headers={**MCP_HEADERS, **_bearer(secret)}, json=body)


def test_hosted_mcp_tools_act_as_the_token_with_its_scopes(auth_client, client, other_account, own_day, monkeypatch):
    """Tool calls loop back to the REST API. They used to carry the server key, so any
    caller reached the server key's account; now they carry the caller's own token."""
    import app.mcp_endpoint as endpoint

    # The production hook on the in-process client: a tool runs on a worker thread,
    # so this also proves the caller's secret reaches it there.
    rest = TestClient(app)
    rest.portal = client.portal
    rest.event_hooks = {"request": [endpoint._forward_caller_secret]}
    monkeypatch.setattr(virgil_mcp, "_client", rest)

    theirs = _token(other_account, ["read"])
    init = _rpc(
        client,
        theirs,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    )
    assert init.status_code == 200, init.text

    read = _rpc(client, theirs, "tools/call", {"name": "get_daily", "arguments": {"date": DAY}})
    assert read.json()["result"]["isError"] is False, read.text
    assert '"energy": 2' in read.text.replace('\\"', '"'), "the other account's day, not the server key's"

    write = _rpc(client, theirs, "tools/call", {"name": "log_measurements", "arguments": {"date": DAY, "weight": 70}})
    assert write.json()["result"]["isError"] is True
    assert "lacks the write scope" in write.text


def _settings_post(client, path: str, data: dict):
    from conftest import csrf_token

    token = csrf_token(client, "/settings?tab=security")
    return client.post(path, data={**data, "_csrf_token": token}, follow_redirects=False)


def test_settings_creates_a_token_once_lists_it_and_revokes_it(auth_client, other_account):
    import re

    created = _settings_post(
        auth_client,
        "/settings/tokens",
        {"name": "Claude Code", "expires": "30", "scope": ["read", "finance"], "password": TEST_PASSWORD},
    )
    assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
    secret = re.search(r'value="(vrg_[^"]+)"', created.text).group(1)
    assert auth_client.get("/api/finance", headers=_bearer(secret)).status_code == 200
    assert auth_client.get("/api/freedom", headers=_bearer(secret)).status_code == 403

    page = auth_client.get("/settings?tab=ai")
    assert "Claude Code" in page.text and secret[:12] in page.text
    assert secret not in page.text, "the secret is shown once, on the response that created it"

    central = _central()
    token_id, expires = central.execute("SELECT id, expires_at FROM api_tokens").fetchone()
    other_id = central.execute("SELECT id FROM users WHERE email = ?", (other_account,)).fetchone()[0]
    central.close()
    assert expires, "30 days, not never"
    theirs = _token(other_account, ["read"])

    central = _central()
    their_id = central.execute("SELECT id FROM api_tokens WHERE user_id = ?", (other_id,)).fetchone()[0]
    central.close()
    refused = _settings_post(auth_client, f"/settings/tokens/{their_id}/revoke", {})
    assert "already revoked" in refused.headers["location"].replace("+", " ")
    assert auth_client.get(f"/api/daily/{DAY}", headers=_bearer(theirs)).status_code == 200, "not yours to revoke"

    assert _settings_post(auth_client, f"/settings/tokens/{token_id}/revoke", {}).status_code == 303
    assert auth_client.get("/api/finance", headers=_bearer(secret)).status_code == 401


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"name": "x", "expires": "90", "scope": ["read"]}, "Password is incorrect"),
        (
            {"name": "x", "expires": "90", "scope": ["read"], "password": "not-it"},  # pragma: allowlist secret
            "Password is incorrect",
        ),
        ({"name": "", "expires": "90", "scope": ["read"], "password": TEST_PASSWORD}, "Give the token a name"),
        ({"name": "x", "expires": "90", "password": TEST_PASSWORD}, "Choose at least one scope"),
        (
            {"name": "x", "expires": "forever", "scope": ["read"], "password": TEST_PASSWORD},
            "Choose when the token expires",
        ),
    ],
    ids=["no-password", "wrong-password", "no-name", "no-scope", "bad-expiry"],
)
def test_settings_refuses_an_incomplete_token(auth_client, data, error):
    from urllib.parse import parse_qs, urlsplit

    response = _settings_post(auth_client, "/settings/tokens", data)
    assert parse_qs(urlsplit(response.headers["location"]).query)["err"][0].startswith(error)
    central = _central()
    assert central.execute("SELECT COUNT(*) FROM api_tokens").fetchone() == (0,)
    central.close()
