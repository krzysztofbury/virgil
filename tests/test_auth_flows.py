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
    client.cookies.set(SESSION_COOKIE, create_session(malformed_user_id, 0))
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

    client.cookies.set(SESSION_COOKIE, create_session("12345678-1234-4234-8234-123456789abc", 0))
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


def _login(client) -> None:
    token = csrf_token(client, "/login")
    resp = client.post(
        "/login",
        data={
            "username": TEST_EMAIL,
            "password": "test-password-123",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _session_is_refused(client, cookie: str) -> bool:
    """Replay a copied session cookie from a separate browser."""
    from app.auth import SESSION_COOKIE

    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, cookie)
    try:
        response = client.get("/", follow_redirects=False)
    finally:
        client.cookies.clear()
    return response.status_code == 303 and response.headers["location"] == "/login"


def test_logout_revokes_every_session_of_the_account(auth_client, client):
    """Clearing the cookie is not enough: a copied token used to stay valid for 7 days."""
    from app.auth import SESSION_COOKIE, _get_signer

    copied = auth_client.cookies[SESSION_COOKIE]
    user_id = _central_conn().execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    # A cookie signed before session epochs existed carries only the account id.
    legacy = _get_signer().sign(user_id).decode()
    assert not _session_is_refused(client, copied)

    token = csrf_token(auth_client, "/settings")
    resp = auth_client.post("/logout", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    set_cookie = resp.headers.get("set-cookie", "")
    assert "virgil_session=;" in set_cookie
    assert "Max-Age=0" in set_cookie

    assert _session_is_refused(client, copied)
    assert _session_is_refused(client, legacy)
    # A revoked cookie is not cleared (a late poll must not wipe a fresh one), so
    # /login has to see it as signed out instead of bouncing it back to /.
    client.cookies.set(SESSION_COOKIE, copied)
    try:
        assert client.get("/login", follow_redirects=False).status_code == 200
    finally:
        client.cookies.clear()
    # Restore the session for the rest of the shared-fixture suite.
    _login(auth_client)
    assert not _session_is_refused(client, auth_client.cookies[SESSION_COOKIE])


def test_disabling_an_account_revokes_sessions_that_reenabling_does_not_restore(auth_client, client):
    import uuid

    from app.auth import create_session

    user_id = str(uuid.uuid4())
    central = _central_conn()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, 'disabled@example.com', 'x', ?)",
        (user_id, f"{user_id}.db"),
    )
    central.commit()
    cookie = create_session(user_id, 0)
    try:
        for action in ("disable", "enable"):
            token = csrf_token(auth_client, "/admin/users")
            resp = auth_client.post(f"/admin/users/{user_id}/{action}", data={"_csrf_token": token})
            assert resp.status_code == 200
        assert central.execute("SELECT is_active FROM users WHERE id = ?", (user_id,)).fetchone()[0] == 1

        assert _session_is_refused(client, cookie)
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()


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

    from app.auth import SESSION_COOKIE
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

        other_browser = auth_client.cookies[SESSION_COOKIE]
        token = csrf_token(auth_client, "/settings/mfa")
        enabled = auth_client.post(
            "/settings/mfa/enable",
            data={"totp_code": pyotp.TOTP(secret).now(), "_csrf_token": token},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        # Turning MFA on signs out every other session; this browser keeps going.
        assert _session_is_refused(client, other_browser)
        assert auth_client.get("/settings").status_code == 200
        stored = central.execute(
            "SELECT totp_secret, totp_enabled FROM users WHERE email = ?",
            (TEST_EMAIL,),
        ).fetchone()
        assert stored[1] == 1
        assert decrypt(stored[0]) == secret

        other_browser = auth_client.cookies[SESSION_COOKIE]
        token = csrf_token(auth_client, "/settings/mfa")
        disabled = auth_client.post(
            "/settings/mfa/disable",
            data={"password": "test-password-123", "_csrf_token": token},  # pragma: allowlist secret
            follow_redirects=False,
        )
        assert disabled.status_code == 303
        assert _session_is_refused(client, other_browser)
        assert auth_client.get("/settings").status_code == 200
    finally:
        central.execute("UPDATE users SET totp_secret = '', totp_enabled = 0 WHERE email = ?", (TEST_EMAIL,))
        central.commit()
        central.close()


def test_an_unfinished_mfa_login_can_start_over_instead_of_looping(client, monkeypatch):
    """Leaving /mfa/verify for /login must show the login form. /login used to treat the
    pending session as signed in and send it to /, which sent it back to /login."""
    from app.auth import SESSION_COOKIE, create_mfa_pending_session

    client.cookies.set(SESSION_COOKIE, create_mfa_pending_session("12345678-1234-1234-1234-123456789abc"))
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


def _wrong_code(secret: str) -> str:
    """A code outside the one-step window /mfa/verify accepts."""
    from datetime import datetime, timedelta

    import pyotp

    totp = pyotp.TOTP(secret)
    now = datetime.now()
    valid = {totp.at(now + timedelta(seconds=30 * step)) for step in (-1, 0, 1)}
    return next(code for code in ("000000", "111111", "222222", "333333") if code not in valid)


def test_mfa_verification_locks_the_account_after_repeated_wrong_codes(client):
    """The per-IP limit alone let a rotating attacker keep guessing six digits."""
    import pyotp

    import app.rate_limit as rate_limit
    from app.routers.auth import MFA_MAX_ATTEMPTS
    from app.services.encryption import encrypt

    secret = pyotp.random_base32()
    central = _central_conn()
    central.execute("UPDATE users SET totp_secret = ?, totp_enabled = 1 WHERE email = ?", (encrypt(secret), TEST_EMAIL))
    central.commit()
    client.cookies.clear()

    try:
        _login(client)
        # One GET: /mfa/verify shares the per-IP limit of 10 a minute with /login.
        token = csrf_token(client, "/mfa/verify")

        def verify(code: str):
            return client.post("/mfa/verify", data={"totp_code": code, "_csrf_token": token}, follow_redirects=False)

        for _ in range(MFA_MAX_ATTEMPTS):
            wrong = verify(_wrong_code(secret))
            assert wrong.status_code == 200
            assert "Invalid code" in wrong.text

        locked = verify(pyotp.TOTP(secret).now())
        assert locked.status_code == 429
        assert "Too many invalid codes" in locked.text

        # Once the lock expires, the right code signs in and clears the count.
        central.execute("UPDATE users SET mfa_locked_until = '2000-01-01T00:00:00Z' WHERE email = ?", (TEST_EMAIL,))
        central.commit()
        rate_limit._buckets.clear()
        assert verify(_wrong_code(secret)).status_code == 200
        signed_in = verify(pyotp.TOTP(secret).now())
        assert signed_in.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 200
        assert central.execute(
            "SELECT mfa_failed_attempts, mfa_locked_until FROM users WHERE email = ?", (TEST_EMAIL,)
        ).fetchone() == (0, "")
    finally:
        client.cookies.clear()
        central.execute(
            "UPDATE users SET totp_secret = '', totp_enabled = 0, mfa_failed_attempts = 0, mfa_locked_until = '' "
            "WHERE email = ?",
            (TEST_EMAIL,),
        )
        central.commit()
        central.close()


def test_parallel_mfa_guesses_stay_within_the_limit(client):
    """Each attempt is counted before its code is checked, so a burst of parallel
    guesses cannot all slip in before the first failure is recorded."""
    import asyncio
    from datetime import UTC, datetime

    from app.central_db import claim_mfa_attempt
    from app.routers.auth import MFA_LOCK_SECONDS, MFA_MAX_ATTEMPTS

    central = _central_conn()
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    now = datetime.now(UTC)

    async def burst():
        return await asyncio.gather(
            *(claim_mfa_attempt(user_id, now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS) for _ in range(12))
        )

    try:
        allowed = client.portal.call(burst)
        assert sum(allowed) == MFA_MAX_ATTEMPTS
        locked_until = central.execute("SELECT mfa_locked_until FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        assert locked_until > now.strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        central.execute("UPDATE users SET mfa_failed_attempts = 0, mfa_locked_until = '' WHERE id = ?", (user_id,))
        central.commit()
        central.close()
