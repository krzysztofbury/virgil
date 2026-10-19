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


def test_a_session_for_a_deleted_account_is_dropped_not_looped(client):
    """/login turns away any validly signed session, so a cookie whose account no
    longer exists must be cleared, or the browser bounces between / and /login."""
    from app.auth import SESSION_COOKIE, create_session

    client.cookies.set(SESSION_COOKIE, create_session("12345678-1234-4234-8234-123456789abc"))
    try:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert f"{SESSION_COOKIE}=;" in response.headers["set-cookie"]
        assert "Max-Age=0" in response.headers["set-cookie"]
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
        data={
            "username": TEST_EMAIL,
            "password": "test-password-123",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": login_token,
        },
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
            "password": "second-password-123",  # pragma: allowlist secret - synthetic fixture
            "password_confirm": "second-password-123",  # pragma: allowlist secret - synthetic fixture
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
        # The setup page shows the manual-entry secret in a <code> block.
        # The QR is a separate same-origin image; the secret is not in its URL.
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


def test_user_db_closes_when_feature_flags_fail(auth_client, monkeypatch):
    """Flag loading used to run before the try/finally that closes the user
    database, so a failure leaked the connection and its thread."""
    from app.user_db import close_user_db as real_close
    from app.user_db import open_user_db as real_open

    opened: list = []
    closed: list = []

    async def tracking_open(filename):
        db = await real_open(filename)
        opened.append(db)
        return db

    async def tracking_close(db):
        closed.append(db)
        await real_close(db)

    async def broken_flags(_db):
        raise RuntimeError("flags unavailable")

    monkeypatch.setattr("app.user_db.open_user_db", tracking_open)
    monkeypatch.setattr("app.user_db.close_user_db", tracking_close)
    monkeypatch.setattr("app.db.get_feature_flags", broken_flags)
    try:
        with pytest.raises(RuntimeError, match="flags unavailable"):
            auth_client.get("/daily")
        assert opened, "the request must open the user database before reading flags"
        assert closed == opened, "the user database must be closed when flag loading raises"
    finally:
        for db in opened:
            if db not in closed:
                auth_client.portal.call(real_close, db)


def _png_pixels(data: bytes) -> bytes:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return img.tobytes()


def test_mfa_setup_never_puts_the_totp_secret_in_a_url(auth_client, client):
    """The QR is built for the signed-in user. The otpauth URI must not ride in a query string,
    and neither the page nor the image may be cached."""
    import io

    import pyotp
    import qrcode

    from app.services.encryption import decrypt

    central = _central_conn()
    try:
        page = auth_client.get("/settings/mfa")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "otpauth" not in page.text.lower()
        assert "/mfa/qr.png?" not in page.text
        assert 'src="/mfa/qr.png"' in page.text
        shown = re.search(r"<code[^>]*>([A-Z2-7]{16,})</code>", page.text)
        assert shown, "MFA setup page must show the base32 secret for manual entry"
        secret = shown.group(1)
        assert not re.search(r"""(?:src|href)=["'][^"']*""" + re.escape(secret), page.text)

        attacker = "otpauth://totp/Evil:attacker@example.com?secret=AAAAAAAAAAAAAAAA&issuer=Evil"
        image = auth_client.get("/mfa/qr.png", params={"uri": attacker})
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.headers["cache-control"] == "no-store"
        expected_uri = pyotp.TOTP(secret).provisioning_uri(name=TEST_EMAIL, issuer_name="Virgil")
        expected = io.BytesIO()
        qrcode.make(expected_uri, box_size=6, border=2).save(expected, format="PNG")
        attacker_png = io.BytesIO()
        qrcode.make(attacker, box_size=6, border=2).save(attacker_png, format="PNG")
        assert _png_pixels(image.content) == _png_pixels(expected.getvalue())
        assert _png_pixels(image.content) != _png_pixels(attacker_png.getvalue())

        assert client.get("/mfa/qr.png", follow_redirects=False).status_code == 303

        bad = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"
        token = csrf_token(auth_client, "/settings/mfa")
        rejected = auth_client.post(
            "/settings/mfa/enable",
            data={"totp_code": bad, "_csrf_token": token},
        )
        assert rejected.status_code == 200
        assert "Invalid code" in rejected.text
        assert rejected.headers["cache-control"] == "no-store"
        assert "otpauth" not in rejected.text.lower()
        assert "/mfa/qr.png?" not in rejected.text

        token = csrf_token(auth_client, "/settings/mfa")
        enabled = auth_client.post(
            "/settings/mfa/enable",
            data={"totp_code": pyotp.TOTP(secret).now(), "_csrf_token": token},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        stored = central.execute(
            "SELECT totp_secret, totp_enabled FROM users WHERE email = ?",
            (TEST_EMAIL,),
        ).fetchone()
        assert stored[1] == 1
        assert decrypt(stored[0]) == secret
    finally:
        central.execute("UPDATE users SET totp_secret = '', totp_enabled = 0 WHERE email = ?", (TEST_EMAIL,))
        central.commit()
        central.close()


def test_an_unfinished_mfa_login_can_start_over_instead_of_looping(client, monkeypatch):
    """Leaving /mfa/verify for /login must show the login form. /login used to treat the
    pending session as signed in and send it to /, which sent it back to /login."""
    from app.auth import MFA_PENDING_PREFIX, SESSION_COOKIE, create_session

    client.cookies.set(SESSION_COOKIE, create_session(f"{MFA_PENDING_PREFIX}12345678-1234-1234-1234-123456789abc"))
    try:
        assert client.get("/login", follow_redirects=False).status_code == 200
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert client.get("/mfa/verify", follow_redirects=False).status_code == 200

        # A pending login lives minutes, not the full session lifetime.
        monkeypatch.setattr("app.auth.MFA_PENDING_MAX_AGE_SECONDS", -1)
        expired = client.get("/mfa/verify", follow_redirects=False)
        assert expired.status_code == 303 and expired.headers["location"].startswith("/login")
    finally:
        client.cookies.delete(SESSION_COOKIE)


def test_a_missing_user_database_is_not_recreated(auth_client, client):
    """aiosqlite.connect creates a missing file. A request that read the account
    just before a factory reset or delete then opened the old name and left an
    empty, unmigrated database behind."""
    import asyncio

    from app.user_db import UserDatabaseMissing, open_user_db

    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    missing = "00000000-0000-4000-8000-000000000000.db"
    with pytest.raises(UserDatabaseMissing):
        asyncio.run(open_user_db(missing))
    assert not os.path.exists(os.path.join(users_dir, missing))

    central = _central_conn()
    current = central.execute("SELECT db_filename FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.execute("UPDATE users SET db_filename = ? WHERE email = ?", (missing, TEST_EMAIL))
    central.commit()
    try:
        response = auth_client.get("/daily", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert not os.path.exists(os.path.join(users_dir, missing))
    finally:
        central.execute("UPDATE users SET db_filename = ? WHERE email = ?", (current, TEST_EMAIL))
        central.commit()
        central.close()


def test_deleting_an_account_removes_its_database_and_backups(auth_client):
    import uuid

    from app.services.backup import BACKUP_DIR

    user_id = str(uuid.uuid4())
    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    (BACKUP_DIR / "pre-migration").mkdir(parents=True, exist_ok=True)
    files = [
        os.path.join(users_dir, f"{user_id}.db"),
        BACKUP_DIR / f"{user_id}-2026-10-01T0000.db",
        BACKUP_DIR / "pre-migration" / f"{user_id}-pre-migration-v036.db",
    ]
    kept = BACKUP_DIR / "22222222-2222-4222-8222-222222222222-2026-10-01T0000.db"
    for path in [*files, kept]:
        open(path, "wb").close()
    central = _central_conn()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, 'gone@example.com', 'x', ?)",
        (user_id, f"{user_id}.db"),
    )
    central.commit()
    try:
        token = csrf_token(auth_client, "/admin/users")
        response = auth_client.post(f"/admin/users/{user_id}/delete", data={"_csrf_token": token})
        assert response.status_code == 200

        assert [str(path) for path in files if os.path.exists(path)] == []
        assert kept.exists()
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()
        kept.unlink(missing_ok=True)
