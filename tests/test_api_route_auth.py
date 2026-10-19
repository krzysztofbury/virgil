"""Who may call each REST route: the API key for all of them, the sensitive scope for Freedom.

One table per contract, built from the router itself, so a new route is checked
the day it is added. These replace per-route copies spread over the API tests.
"""

import re
import sqlite3

import pytest
from conftest import user_db_path
from fastapi.testclient import TestClient

from app.main import app
from app.routers.api import router

KEY = {"X-API-Key": "test-key-123"}

# Values that name nothing in the shared database: nothing can be read or changed.
_SAMPLES = {"day": "1999-01-01", "habit": "porn"}


def _url(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: _SAMPLES.get(m.group(1), "999999"), path)


ROUTES = sorted((method, route.path) for route in router.routes for method in route.methods)


@pytest.mark.parametrize(("method", "path"), ROUTES, ids=[f"{m} {p}" for m, p in ROUTES])
def test_every_route_answers_401_without_the_key(client, method, path):
    """MCP and curl send only X-API-Key, never a session cookie or a CSRF token. A
    route the auth or CSRF middleware does not exempt would answer a login redirect
    or a 403 instead of reaching its own key check."""
    anonymous = TestClient(app)
    anonymous.portal = client.portal
    response = anonymous.request(method, _url(path), json={}, follow_redirects=False)
    assert response.status_code == 401


# Freedom data is intimate. Without VIRGIL_API_SENSITIVE the key alone must not reach it.
SENSITIVE = {
    ("GET", "/api/noporn"),
    ("GET", "/api/freedom"),
    ("PUT", "/api/freedom/{habit}/days/{day}"),
    ("DELETE", "/api/freedom/{habit}/days/{day}"),
    ("POST", "/api/freedom/{habit}/bricks"),
    ("PATCH", "/api/freedom/bricks/{brick_id}"),
    ("DELETE", "/api/freedom/bricks/{brick_id}"),
    ("PATCH", "/api/freedom/{habit}"),
    ("PUT", "/api/feniks/daily/{day}"),
    ("DELETE", "/api/feniks/daily/{day}"),
    ("POST", "/api/feniks/bricks"),
    ("PATCH", "/api/feniks/bricks/{brick_id}"),
    ("DELETE", "/api/feniks/bricks/{brick_id}"),
    ("PATCH", "/api/feniks/config"),
}
# A valid body, so the refusal comes from the scope and not from validation.
_BODIES = {"PUT": {"slipped": False}, "POST": {"date": "1999-01-01", "hook": "x"}, "PATCH": {"hook": "x"}}
_SETTINGS_BODY = {"target_days": 30}


@pytest.mark.parametrize(("method", "path"), sorted(SENSITIVE), ids=[f"{m} {p}" for m, p in sorted(SENSITIVE)])
def test_freedom_routes_refuse_the_key_without_the_sensitive_scope(auth_client, method, path):
    assert (method, path) in ROUTES, "the table names a route the router no longer has"
    body = _SETTINGS_BODY if path in ("/api/freedom/{habit}", "/api/feniks/config") else _BODIES.get(method)
    response = auth_client.request(method, _url(path), json=body, headers=KEY)
    assert response.status_code == 403
    assert "Sensitive scope" in response.json()["detail"]
    # Refused before anything was written for the sample day.
    conn = sqlite3.connect(user_db_path())
    try:
        written = conn.execute(
            "SELECT (SELECT COUNT(*) FROM feniks_daily WHERE date = ?) + (SELECT COUNT(*) FROM feniks_bricks WHERE date = ?)",
            (_SAMPLES["day"], _SAMPLES["day"]),
        ).fetchone()[0]
    finally:
        conn.close()
    assert written == 0


@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "GET" and ("GET", p) not in SENSITIVE], ids=lambda p: p)
def test_other_reads_do_not_need_the_sensitive_scope(auth_client, path):
    """The other direction: a read missing from the table above answers without the scope."""
    assert auth_client.get(_url(path), headers=KEY).status_code != 403


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [("POST", "/api/briefing/generate", 303), ("POST", "/mcp", 401)],
    ids=["session-route-under-api", "mcp"],
)
def test_only_api_key_routes_skip_the_session(client, method, path, status):
    """The exemption comes from the routes that depend on the key. A session route
    that merely lives under /api must still send an anonymous caller to log in."""
    anonymous = TestClient(app)
    anonymous.portal = client.portal
    response = anonymous.request(method, path, follow_redirects=False)
    assert response.status_code == status
