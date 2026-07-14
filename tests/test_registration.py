"""Registration gating: closed by default, but the first account bootstraps."""

import app.routers.auth as auth_module


def test_signup_closed_shows_message(auth_client, monkeypatch):
    """With registration closed and a user existing, /signup must say so."""
    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", False)
    resp = auth_client.get("/signup")
    assert resp.status_code == 200
    assert "Registration is closed" in resp.text


def test_signup_post_blocked_when_closed(auth_client, monkeypatch):
    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", False)
    from conftest import csrf_token

    token = csrf_token(auth_client, "/login")
    resp = auth_client.post(
        "/signup",
        data={
            "email": "intruder@example.com",
            "password": "password-123",
            "password_confirm": "password-123",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"

    import os
    import sqlite3

    central = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
    try:
        row = central.execute("SELECT 1 FROM users WHERE email = ?", ("intruder@example.com",)).fetchone()
        assert row is None
    finally:
        central.close()


def _anonymous_client():
    """Fresh client with no session cookie — the shared fixture is logged in
    and would be redirected off /login."""
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


def test_login_hides_signup_link_when_closed(auth_client, monkeypatch):
    # auth_client guarantees a user exists, so bootstrap doesn't reopen signup.
    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", False)
    resp = _anonymous_client().get("/login")
    assert resp.status_code == 200
    assert 'href="/signup"' not in resp.text


def test_login_shows_signup_link_when_open(monkeypatch):
    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", True)
    resp = _anonymous_client().get("/login")
    assert resp.status_code == 200
    assert 'href="/signup"' in resp.text


def test_login_empty_password_is_rejected_not_500(client):
    """verify_password raises on empty input — the route must catch that."""
    from conftest import csrf_token

    token = csrf_token(client, "/login")
    resp = client.post(
        "/login",
        data={"username": "test@example.com", "password": "", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "Invalid email or password" in resp.text


def test_csrf_non_ascii_token_is_403_not_500(client):
    """compare_digest on str raises TypeError for non-ASCII — must 403 cleanly."""
    client.get("/login")  # ensure a csrf cookie exists
    resp = client.post(
        "/login",
        data={"username": "x@y.z", "password": "irrelevant1", "_csrf_token": "żółć-non-ascii"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_bootstrap_allows_first_user(monkeypatch):
    """registration_allowed() is True on a fresh install even when closed."""
    import asyncio

    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", False)

    async def no_users():
        return 0

    monkeypatch.setattr(auth_module, "count_users", no_users)
    assert asyncio.run(auth_module.registration_allowed()) is True
