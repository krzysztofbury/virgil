"""One-time recovery codes for two-factor sign-in (#168)."""

import asyncio
import re
import sqlite3
from pathlib import Path

import pyotp
import pytest
from conftest import _TMP, TEST_EMAIL, TEST_PASSWORD, csrf_token

from app.central_db import count_recovery_codes, replace_recovery_codes

CODE = re.compile(r"<code>([a-z2-9]{5}-[a-z2-9]{5})</code>")


def _central():
    conn = sqlite3.connect(Path(_TMP) / "central.db")
    conn.row_factory = sqlite3.Row
    return conn


def _user_id(central) -> str:
    return central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()["id"]


@pytest.fixture
def totp_account(auth_client):
    """The test account with TOTP on and a fresh set of codes; reset afterwards."""
    from app.services.encryption import encrypt

    secret = pyotp.random_base32()
    central = _central()
    user_id = _user_id(central)
    central.execute("UPDATE users SET totp_secret = ?, totp_enabled = 1 WHERE id = ?", (encrypt(secret), user_id))
    central.commit()
    codes = asyncio.run(replace_recovery_codes(user_id))
    try:
        yield user_id, secret, codes, central
    finally:
        central.execute(
            "UPDATE users SET totp_secret = '', totp_enabled = 0, mfa_failed_attempts = 0, mfa_locked_until = '' "
            "WHERE id = ?",
            (user_id,),
        )
        central.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))
        central.commit()
        central.close()


def _pending_login(client):
    client.cookies.clear()
    token = csrf_token(client, "/login")
    client.post("/login", data={"username": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token})
    return csrf_token(client, "/mfa/verify")


def _recover(client, token: str, code: str):
    return client.post("/mfa/verify", data={"recovery_code": code, "_csrf_token": token}, follow_redirects=False)


def test_enabling_mfa_shows_ten_codes_once(auth_client):
    central = _central()
    user_id = _user_id(central)
    try:
        page = auth_client.get("/settings/mfa").text
        secret = re.search(r"<code[^>]*>([A-Z2-7]{16,})</code>", page).group(1)
        enabled = auth_client.post(
            "/settings/mfa/enable",
            data={"totp_code": pyotp.TOTP(secret).now(), "_csrf_token": csrf_token(auth_client, "/settings/mfa")},
            follow_redirects=False,
        )
        assert enabled.status_code == 200
        assert enabled.headers["cache-control"] == "no-store"
        assert len(set(CODE.findall(enabled.text))) == 10
        assert "virgil_session=" in enabled.headers["set-cookie"]
        assert asyncio.run(count_recovery_codes(user_id)) == 10
        assert auth_client.get("/settings").status_code == 200
        assert not CODE.search(auth_client.get("/settings?tab=security").text), "codes are shown once"
    finally:
        central.execute("UPDATE users SET totp_secret = '', totp_enabled = 0 WHERE id = ?", (user_id,))
        central.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))
        central.commit()
        central.close()


def test_a_recovery_code_signs_in_instead_of_the_authenticator(client, totp_account):
    user_id, _secret, codes, central = totp_account
    token = _pending_login(client)
    try:
        signed_in = _recover(client, token, codes[0])
        assert signed_in.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 200
        assert (
            central.execute(
                "SELECT COUNT(*) FROM auth_events WHERE user_id = ? AND event = 'recovery_code_use'", (user_id,)
            ).fetchone()[0]
            >= 1
        )
    finally:
        client.cookies.clear()


def test_a_recovery_code_is_forgiving_about_format_and_works_once(client, totp_account):
    _user_id_, _secret, codes, _central_ = totp_account
    sloppy = " " + codes[1].upper().replace("-", " ") + " "
    try:
        assert _recover(client, _pending_login(client), sloppy).status_code == 303
        again = _recover(client, _pending_login(client), codes[1])
        assert again.status_code == 200 and "Invalid code" in again.text
    finally:
        client.cookies.clear()


def test_wrong_recovery_codes_count_toward_the_lockout(client, totp_account):
    from app.routers.auth import MFA_MAX_ATTEMPTS

    _user_id_, _secret, codes, _central_ = totp_account
    token = _pending_login(client)
    try:
        for _ in range(MFA_MAX_ATTEMPTS):
            assert _recover(client, token, "aaaaa-aaaaa").status_code == 200
        assert _recover(client, token, codes[2]).status_code == 429
    finally:
        client.cookies.clear()


def test_new_codes_void_the_old_ones(auth_client, totp_account):
    _user_id_, secret, codes, _central_ = totp_account
    page = auth_client.post(
        "/settings/mfa/recovery-codes",
        data={"totp_code": pyotp.TOTP(secret).now(), "_csrf_token": csrf_token(auth_client, "/settings?tab=security")},
    )
    fresh = set(CODE.findall(page.text))
    assert len(fresh) == 10 and not fresh & set(codes)
    assert page.headers["cache-control"] == "no-store"


def test_new_codes_need_a_current_code(auth_client, totp_account):
    _user_id_, _secret, codes, _central_ = totp_account
    page = auth_client.post(
        "/settings/mfa/recovery-codes",
        data={"totp_code": "000000", "_csrf_token": csrf_token(auth_client, "/settings?tab=security")},
    )
    assert not CODE.search(page.text.replace(codes[0], ""))


def test_the_security_tab_offers_codes_to_an_account_without_them(auth_client, totp_account):
    user_id, _secret, _codes, central = totp_account
    assert "10 left" in auth_client.get("/settings?tab=security").text
    central.execute("DELETE FROM mfa_recovery_codes WHERE user_id = ?", (user_id,))
    central.commit()
    assert "Create recovery codes" in auth_client.get("/settings?tab=security").text


def test_disabling_mfa_deletes_the_codes(auth_client, totp_account):
    user_id, _secret, _codes, _central_ = totp_account
    auth_client.post(
        "/settings/mfa/disable",
        data={"password": TEST_PASSWORD, "_csrf_token": csrf_token(auth_client, "/settings/mfa")},
    )
    assert asyncio.run(count_recovery_codes(user_id)) == 0
