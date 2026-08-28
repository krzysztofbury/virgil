"""Auth-adjacent P0 coverage: OAuth callback state, logout, multi-user isolation,
TOTP secret encryption at rest."""

import os
import re
import sqlite3

import pytest
from conftest import TEST_EMAIL, csrf_token
from fastapi import HTTPException


def _central_conn():
    return sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])


def test_anonymous_client_stays_anonymous_after_auth_fixture(client, auth_client):
    """Using auth_client must not leak its session into the anonymous client."""
    assert auth_client.portal is client.portal
    assert auth_client.get("/").status_code == 200

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_admin_user_id_rejects_trailing_newline():
    from app.routers.admin import _validate_user_id

    valid_user_id = "12345678-1234-1234-1234-123456789abc"
    _validate_user_id(valid_user_id)
    with pytest.raises(HTTPException, match="Invalid user ID"):
        _validate_user_id(valid_user_id + "\n")


def test_auth_middleware_rejects_signed_noncanonical_user_id(client, monkeypatch):
    import app.central_db as central_db
    from app.auth import SESSION_COOKIE, create_session

    lookup_called = False

    async def unexpected_lookup(_user_id):
        nonlocal lookup_called
        lookup_called = True
        return None

    monkeypatch.setattr(central_db, "get_user_by_id", unexpected_lookup)

    malformed_user_id = "12345678-1234-1234-1234-123456789abc\n"
    client.cookies.set(SESSION_COOKIE, create_session(malformed_user_id))
    try:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert lookup_called is False
    finally:
        client.cookies.delete(SESSION_COOKIE)


def test_oura_callback_rejects_missing_or_mismatched_state(auth_client):
    """CSRF-safe OAuth: the callback must bounce (not 500, not proceed) when the
    state cookie is absent or doesn't match."""
    resp = auth_client.get(
        "/settings/oura/callback",
        params={"code": "fake-code", "state": "not-the-cookie-value"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/settings")


def test_logout_clears_session_cookie(auth_client):
    token = csrf_token(auth_client, "/settings")
    resp = auth_client.post("/logout", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    set_cookie = resp.headers.get("set-cookie", "")
    assert "virgil_session=;" in set_cookie
    assert "Max-Age=0" in set_cookie
    # Restore the session for the rest of the shared-fixture suite.
    login_token = csrf_token(auth_client, "/login")
    resp = auth_client.post(
        "/login",
        data={"username": TEST_EMAIL, "password": "test-password-123", "_csrf_token": login_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_second_user_gets_isolated_database(auth_client, monkeypatch):
    """Multi-user isolation: a second account writes to its own DB file and the
    first user's data is untouched."""
    import app.routers.auth as auth_module

    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", True)

    from fastapi.testclient import TestClient

    from app.main import app

    second = TestClient(app)
    token = csrf_token(second, "/signup")
    resp = second.post(
        "/signup",
        data={
            "email": "second@example.com",
            "password": "second-password-123",
            "password_confirm": "second-password-123",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    central = _central_conn()
    try:
        rows = dict(central.execute("SELECT email, db_filename FROM users").fetchall())
        assert len(rows) == 2
        assert rows[TEST_EMAIL] != rows["second@example.com"]
        second_db = rows["second@example.com"]
        first_db = rows[TEST_EMAIL]
    finally:
        central.close()

    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    try:
        # Write into the second user's DB; the first user's DB must not see it.
        conn = sqlite3.connect(os.path.join(users_dir, second_db))
        conn.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-07-02', 9)")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(os.path.join(users_dir, first_db))
        leaked = conn.execute("SELECT COUNT(*) FROM daily_logs WHERE date = '2026-07-02'").fetchone()[0]
        conn.close()
        assert leaked == 0, "Second user's data leaked into the first user's database"
    finally:
        # Cleanup so conftest.user_db_path()'s single-DB assumption holds.
        central = _central_conn()
        central.execute("DELETE FROM users WHERE email = 'second@example.com'")
        central.commit()
        central.close()
        for suffix in ("", "-wal", "-shm"):
            path = os.path.join(users_dir, second_db + suffix)
            if os.path.exists(path):
                os.remove(path)


def test_totp_secret_encrypted_at_rest(auth_client):
    """The central DB must never hold a usable TOTP seed in plaintext."""
    from app.services.encryption import decrypt

    central = _central_conn()
    try:
        page = auth_client.get("/settings/mfa")
        assert page.status_code == 200
        # The setup page shows the manual-entry secret in a <code> block (the
        # provisioning URI itself is URL-encoded inside the QR image src).
        shown = re.search(r"<code[^>]*>([A-Z2-7]{16,})</code>", page.text)
        assert shown, "MFA setup page must show the base32 secret"
        plain_secret = shown.group(1)

        stored = central.execute("SELECT totp_secret FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
        assert stored != plain_secret, "TOTP secret stored in plaintext"
        assert decrypt(stored) == plain_secret
    finally:
        # Leave MFA unconfigured for other tests even if an assert fired.
        central.execute("UPDATE users SET totp_secret = '', totp_enabled = 0 WHERE email = ?", (TEST_EMAIL,))
        central.commit()
        central.close()
