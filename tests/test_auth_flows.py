"""Auth-adjacent P0 coverage: login, MFA, sessions, OAuth callback state and TOTP
secret encryption at rest. Isolation between accounts lives in test_user_isolation."""

import os
import re
import sqlite3

import pytest
from conftest import TEST_EMAIL, TEST_PASSWORD, csrf_token
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
        # Since #168 the answer is the page with the recovery codes, shown once.
        assert enabled.status_code == 200
        assert enabled.text.count("<code>") == 10
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
        # The password alone yields a pending login that only reaches /mfa/verify.
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
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


@pytest.mark.parametrize(
    ("email", "password", "error"),
    [
        (TEST_EMAIL, "not-the-password", "Invalid email or password."),
        ("inactive@example.com", "inactive-password-123", "This account has been deactivated."),
    ],
    ids=["wrong-password", "inactive-account"],
)
def test_login_refuses_without_issuing_a_session(client, email, password, error):
    """Accepting a wrong password, or letting a deactivated account in, both passed
    the whole suite: no test logged in with anything but the right credentials."""
    import uuid

    from app.auth import SESSION_COOKIE, hash_password

    central = _central_conn()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename, is_active) VALUES (?, ?, ?, 'inactive.db', 0)",
        (str(uuid.uuid4()), "inactive@example.com", hash_password("inactive-password-123")),
    )
    central.commit()
    client.cookies.clear()
    try:
        token = csrf_token(client, "/login")
        response = client.post(
            "/login", data={"username": email, "password": password, "_csrf_token": token}, follow_redirects=False
        )
        assert response.status_code == 200
        assert error in response.text
        assert SESSION_COOKIE not in response.cookies
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    finally:
        client.cookies.clear()
        central.execute("DELETE FROM users WHERE email = 'inactive@example.com'")
        central.commit()
        central.close()


def _feedback(response) -> dict:
    from urllib.parse import parse_qs, urlsplit

    assert response.status_code == 303
    return {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}


def _change_password(client, current: str, new: str, confirm: str | None = None):
    token = csrf_token(client, "/settings?tab=security")
    return client.post(
        "/settings/password",
        data={
            "current_password": current,
            "new_password": new,
            "new_password_confirm": new if confirm is None else confirm,
            "_csrf_token": token,
        },
        follow_redirects=False,
    )


def test_a_password_change_signs_out_other_devices_and_keeps_this_one(auth_client, client):
    from app.auth import SESSION_COOKIE, hash_password

    new_password = "a-new-password-456"  # pragma: allowlist secret - synthetic fixture
    central = _central_conn()
    stored = central.execute("SELECT password_hash FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    other_device = auth_client.cookies[SESSION_COOKIE]
    try:
        refused = [
            _change_password(auth_client, "not-the-password", new_password),
            _change_password(auth_client, TEST_PASSWORD, "short", "short"),
            _change_password(auth_client, TEST_PASSWORD, new_password, "a-different-one-789"),
        ]
        assert [_feedback(response)["err"] for response in refused] == [
            "Current password is incorrect.",
            "New password must be at least 8 characters.",
            "New passwords do not match.",
        ]
        assert central.execute("SELECT password_hash FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0] == stored
        assert not _session_is_refused(client, other_device)

        changed = _change_password(auth_client, TEST_PASSWORD, new_password)
        assert _feedback(changed)["msg"] == "Password changed. Other devices are signed out."
        assert _session_is_refused(client, other_device)
        assert auth_client.get("/settings", follow_redirects=False).status_code == 200, "this browser stays in"

        for password, signed_in in ((TEST_PASSWORD, False), (new_password, True)):
            client.cookies.clear()
            token = csrf_token(client, "/login")
            response = client.post(
                "/login",
                data={"username": TEST_EMAIL, "password": password, "_csrf_token": token},
                follow_redirects=False,
            )
            assert (response.status_code == 303) is signed_in, password
        client.cookies.clear()
    finally:
        central.execute(
            "UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(TEST_PASSWORD), TEST_EMAIL)
        )
        central.commit()
        central.close()
        _login(auth_client)


def test_sign_ins_and_account_changes_are_logged_and_old_events_pruned(auth_client, client):
    import asyncio

    from app.central_db import prune_auth_events

    central = _central_conn()
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.execute("DELETE FROM auth_events")
    central.execute(
        "INSERT INTO auth_events (created_at, user_id, event) VALUES (datetime('now', '-181 days'), ?, 'login_ok')",
        (user_id,),
    )
    central.commit()
    try:
        client.cookies.clear()
        for password in ("not-the-password", TEST_PASSWORD):
            token = csrf_token(client, "/login")
            client.post(
                "/login",
                data={"username": TEST_EMAIL, "password": password, "_csrf_token": token},
                follow_redirects=False,
            )
        token = csrf_token(client, "/login")
        client.post(
            "/login",
            data={
                "username": "nobody@example.com",
                "password": "whatever-123",  # pragma: allowlist secret - synthetic fixture
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        client.cookies.clear()

        rows = central.execute(
            "SELECT event, user_id, ip, detail FROM auth_events WHERE created_at > datetime('now', '-1 day') ORDER BY id"
        ).fetchall()
        assert [(event, uid, detail) for event, uid, _ip, detail in rows] == [
            ("login_fail", user_id, "wrong password"),
            ("login_ok", user_id, ""),
            ("login_fail", None, "unknown account"),
        ], "an unknown address is never stored: it may be a password typed in the wrong field"
        assert all(ip for _event, _uid, ip, _detail in rows)

        page = auth_client.get("/admin/users")
        assert "Security events" in page.text and "login fail" in page.text

        assert asyncio.run(prune_auth_events()) == 1
        assert central.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0] == 3
    finally:
        central.execute("DELETE FROM auth_events")
        central.commit()
        central.close()


def test_only_the_first_account_may_use_the_server_llm_key_by_default(tmp_path, monkeypatch):
    import asyncio

    import app.central_db as central_db

    monkeypatch.setattr(central_db, "CENTRAL_DB_PATH", str(tmp_path / "central.db"))
    monkeypatch.setattr(central_db, "_central_db", None)

    async def run():
        try:
            await central_db.init_central_db()
            owner = await central_db.create_user("owner@example.com", "owner-password-1")
            invited = await central_db.create_user("invited@example.com", "invited-password-1")
            allowed = await central_db.create_user("allowed@example.com", "allowed-password-1", allow_shared_llm=True)
            return [user["allow_shared_llm"] for user in (owner, invited, allowed)]
        finally:
            await central_db.close_central_db()

    assert asyncio.run(run()) == [1, 0, 1]
