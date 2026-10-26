"""Accounts without a password (#168): created only for a proven address."""

import asyncio
import re
import sqlite3
import uuid
from pathlib import Path

import pyotp
import pytest
from conftest import _TMP, TEST_EMAIL, csrf_token
from fastapi.testclient import TestClient

from app import config
from app.central_db import create_invite

FINISH = "Check your email to finish creating your account."


def _central():
    conn = sqlite3.connect(Path(_TMP) / "central.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def browser(client):
    """A fresh cookie jar against the running app."""
    from app.main import app

    fresh = TestClient(app)
    fresh.portal = client.portal
    try:
        yield fresh
    finally:
        fresh.close()


@pytest.fixture
def cleanup():
    emails: list[str] = []
    yield emails
    central = _central()
    for email in emails:
        row = central.execute("SELECT id, db_filename FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            central.execute("DELETE FROM users WHERE id = ?", (row["id"],))
            (Path(_TMP) / "users" / row["db_filename"]).unlink(missing_ok=True)
        central.execute("DELETE FROM sign_in_links WHERE email = ?", (email,))
    central.commit()
    central.close()


def _admin_id() -> str:
    central = _central()
    try:
        return central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()["id"]
    finally:
        central.close()


def _invite(email: str = "") -> str:
    return asyncio.run(create_invite(_admin_id(), email=email))


def _signup(browser, invite: str, email: str, method: str = "link", password: str | None = None):
    page = f"/signup?invite={invite}" if invite else "/signup"
    data = {"email": email, "display_name": "Ala", "sign_in_method": method, "_csrf_token": csrf_token(browser, page)}
    if invite:
        data["invite"] = invite
    if password:
        data |= {"password": password, "password_confirm": password}
    return browser.post("/signup", data=data, follow_redirects=False)


def _open_link(browser, mail) -> object:
    token = re.search(r"token=([\w-]+)", mail.text).group(1)
    page = browser.get(f"/login/link?token={token}")
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', page.text).group(1)
    return browser.post("/login/link/confirm", data={"token": token, "_csrf_token": csrf}, follow_redirects=False)


def _hash_of(email: str):
    central = _central()
    try:
        row = central.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
        return None if row is None else row["password_hash"]
    finally:
        central.close()


def test_an_invited_address_can_sign_up_without_a_password(auth_client, browser, sent_mail, cleanup):
    email = f"invited-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    response = _signup(browser, _invite(email), email)
    assert response.status_code == 303 and response.headers["location"].startswith("/onboarding")
    assert _hash_of(email) == ""
    assert sent_mail == []


def test_a_typed_address_gets_a_link_before_the_account_exists(auth_client, browser, sent_mail, cleanup):
    email = f"typed-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    response = _signup(browser, _invite(""), email)
    assert FINISH in response.text
    assert _hash_of(email) is None, "no account until the address is proven"
    (mail,) = sent_mail
    assert mail.to == email
    created = _open_link(browser, mail)
    assert created.status_code == 303 and created.headers["location"].startswith("/onboarding")
    assert _hash_of(email) == ""


def test_a_mistyped_address_creates_nothing(auth_client, browser, sent_mail, cleanup):
    email = f"typo-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(""), email)
    central = _central()
    central.execute("UPDATE sign_in_links SET expires_at = '2000-01-01T00:00:00Z' WHERE email = ?", (email,))
    central.commit()
    central.close()
    token = re.search(r"token=([\w-]+)", sent_mail[0].text).group(1)
    assert "That link has expired" in browser.get(f"/login/link?token={token}").text
    assert _hash_of(email) is None


def test_with_mail_off_signup_has_no_link_only_choice(auth_client, browser, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    page = browser.get(f"/signup?invite={_invite('')}").text
    assert 'name="sign_in_method"' not in page
    assert 'name="password"' in page


def test_the_link_choice_is_offered_when_mail_is_on(auth_client, browser, sent_mail):
    page = browser.get(f"/signup?invite={_invite('')}").text
    assert 'name="sign_in_method" value="link"' in page and 'name="sign_in_method" value="password"' in page


def test_adding_a_password_to_a_link_only_account(auth_client, browser, sent_mail, cleanup):
    email = f"adder-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    response = browser.post(
        "/settings/password/add",
        data={
            "new_password": "fresh-password-1",  # pragma: allowlist secret - synthetic fixture
            "new_password_confirm": "fresh-password-1",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": csrf_token(browser, "/settings?tab=security"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _hash_of(email).startswith("$2")


def test_removing_the_password_needs_mail_and_the_current_password(auth_client, browser, sent_mail, cleanup):
    email = f"remover-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    password = "remover-password-1"  # pragma: allowlist secret - synthetic fixture
    _signup(browser, _invite(email), email, method="password", password=password)
    _finish_onboarding(email)

    def remove(with_password: str):
        return browser.post(
            "/settings/password/remove",
            data={"current_password": with_password, "_csrf_token": csrf_token(browser, "/settings?tab=security")},
            follow_redirects=False,
        )

    remove("wrong-password-1")
    assert _hash_of(email).startswith("$2")
    remove(password)
    assert _hash_of(email) == ""


def test_removing_the_password_is_refused_with_mail_off(auth_client, browser, sent_mail, cleanup, monkeypatch):
    email = f"keeper-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    password = "keeper-password-1"  # pragma: allowlist secret - synthetic fixture
    _signup(browser, _invite(email), email, method="password", password=password)
    _finish_onboarding(email)
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    browser.post(
        "/settings/password/remove",
        data={"current_password": password, "_csrf_token": csrf_token(browser, "/settings?tab=security")},
    )
    assert _hash_of(email).startswith("$2")


STALE = "For this, sign in again with a new link first."


@pytest.fixture
def stale_sessions(monkeypatch):
    """Every session counts as older than the fresh-sign-in window."""
    import app.auth as auth

    monkeypatch.setattr(auth, "FRESH_SESSION_SECONDS", -1)


def _make_token(browser, **extra):
    return browser.post(
        "/settings/tokens",
        data={
            "name": "laptop",
            "scope": "read",
            "expires": "90",
            "_csrf_token": csrf_token(browser, "/settings?tab=security"),
            **extra,
        },
    )


def test_a_freshly_signed_in_link_only_account_can_make_a_token(auth_client, browser, sent_mail, cleanup):
    """No password and no authenticator: a sign-in from a link minutes ago is the proof."""
    email = f"tokener-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    assert "vrg_" in _make_token(browser).text


def test_a_stale_session_cannot_make_a_token_without_two_factor(
    auth_client, browser, sent_mail, cleanup, stale_sessions
):
    email = f"stale-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    response = _make_token(browser)
    assert STALE in response.text and "vrg_" not in response.text


def test_a_stale_session_cannot_add_a_first_password(auth_client, browser, sent_mail, cleanup, stale_sessions):
    """A stolen session must not become a lasting login."""
    email = f"stalepw-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    browser.post(
        "/settings/password/add",
        data={
            "new_password": "fresh-password-1",  # pragma: allowlist secret - synthetic fixture
            "new_password_confirm": "fresh-password-1",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": csrf_token(browser, "/settings?tab=security"),
        },
    )
    assert _hash_of(email) == ""


def test_a_stale_session_cannot_set_up_two_factor_without_a_password(
    auth_client, browser, sent_mail, cleanup, stale_sessions
):
    """Otherwise a stolen session could enroll its own authenticator and use it
    to approve tokens."""
    email = f"stalemfa-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    page = browser.get("/settings/mfa").text
    secret = re.search(r"<code[^>]*>([A-Z2-7]{16,})</code>", page).group(1)
    browser.post(
        "/settings/mfa/enable",
        data={"totp_code": pyotp.TOTP(secret).now(), "_csrf_token": csrf_token(browser, "/settings/mfa")},
    )
    central = _central()
    enabled = central.execute("SELECT totp_enabled FROM users WHERE email = ?", (email,)).fetchone()[0]
    central.close()
    assert enabled == 0


def test_a_link_only_account_confirms_a_token_with_its_authenticator(auth_client, browser, sent_mail, cleanup):
    from app.services.encryption import encrypt

    email = f"totper-{uuid.uuid4().hex[:8]}@example.com"
    cleanup.append(email)
    _signup(browser, _invite(email), email)
    _finish_onboarding(email)
    secret = pyotp.random_base32()
    central = _central()
    central.execute("UPDATE users SET totp_secret = ?, totp_enabled = 1 WHERE email = ?", (encrypt(secret), email))
    central.commit()
    central.close()
    response = browser.post(
        "/settings/tokens",
        data={
            "name": "laptop",
            "scope": "read",
            "expires": "90",
            "totp_code": pyotp.TOTP(secret).now(),
            "_csrf_token": csrf_token(browser, "/settings?tab=security"),
        },
    )
    assert "vrg_" in response.text


def _finish_onboarding(email: str) -> None:
    central = _central()
    db_filename = central.execute("SELECT db_filename FROM users WHERE email = ?", (email,)).fetchone()["db_filename"]
    central.close()
    user_db = sqlite3.connect(Path(_TMP) / "users" / db_filename)
    user_db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('onboarding_completed', '1')")
    user_db.commit()
    user_db.close()
