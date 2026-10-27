"""Opening an emailed sign-in link (#168): a confirming click, one sign-in."""

import asyncio
import re
import sqlite3
import uuid
from pathlib import Path

import pytest
from conftest import _TMP, csrf_token

from app.central_db import create_sign_in_link

EXPIRED = "That link has expired. Ask for a new one."


@pytest.fixture
def account(client):
    user_id = str(uuid.uuid4())
    email = f"opener-{user_id[:8]}@example.com"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, ?, '', ?)",
        (user_id, email, f"{user_id}.db"),
    )
    central.commit()
    try:
        yield user_id, email, central
    finally:
        central.execute("DELETE FROM sign_in_links WHERE email = ?", (email,))
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()


def _link(user_id: str, email: str) -> str:
    return asyncio.run(create_sign_in_link(email, user_id=user_id))


def _confirm(client, token: str):
    page = client.get(f"/login/link?token={token}")
    csrf = re.search(r'name="_csrf_token" value="([^"]+)"', page.text)
    return client.post(
        "/login/link/confirm",
        data={"token": token, "_csrf_token": csrf.group(1) if csrf else csrf_token(client, "/login")},
        follow_redirects=False,
    )


def test_opening_the_link_does_not_use_it(client, account):
    user_id, email, _central = account
    token = _link(user_id, email)
    for _ in range(2):
        page = client.get(f"/login/link?token={token}")
        assert page.status_code == 200
        assert 'action="/login/link/confirm"' in page.text and token in page.text


def test_the_link_page_is_not_cached(client, account):
    user_id, email, _central = account
    page = client.get(f"/login/link?token={_link(user_id, email)}")
    assert page.headers["cache-control"] == "no-store"


def test_a_link_signs_in_once_even_when_confirmed_twice(client, account):
    user_id, email, central = account
    token = _link(user_id, email)
    first = _confirm(client, token)
    assert first.status_code == 303 and first.headers["location"].startswith("/")
    assert "virgil_session=" in first.headers["set-cookie"]
    client.cookies.clear()
    second = _confirm(client, token)
    assert "virgil_session" not in second.cookies
    assert EXPIRED in client.get(second.headers["location"]).text
    events = central.execute(
        "SELECT COUNT(*) FROM auth_events WHERE user_id = ? AND event = 'link_sign_in'", (user_id,)
    ).fetchone()[0]
    assert events == 1


def test_a_link_for_a_totp_account_asks_for_the_code(client, account):
    user_id, email, central = account
    secret = "JBSWY3DPEHPK3PXP"  # pragma: allowlist secret - synthetic fixture
    central.execute("UPDATE users SET totp_enabled = 1, totp_secret = ? WHERE id = ?", (secret, user_id))
    central.commit()
    response = _confirm(client, _link(user_id, email))
    client.cookies.clear()
    assert response.status_code == 303 and response.headers["location"] == "/mfa/verify"


def test_an_expired_link_says_so(client, account):
    user_id, email, central = account
    token = _link(user_id, email)
    central.execute("UPDATE sign_in_links SET expires_at = '2000-01-01T00:00:00Z' WHERE email = ?", (email,))
    central.commit()
    page = client.get(f"/login/link?token={token}")
    assert EXPIRED in page.text
    assert 'action="/login/link/confirm"' not in page.text


def test_a_link_for_a_disabled_account_does_not_sign_in(client, account):
    user_id, email, central = account
    token = _link(user_id, email)
    central.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    central.commit()
    response = _confirm(client, token)
    assert "virgil_session" not in response.cookies
    assert response.headers["location"].startswith("/login")


AUTO = "data-auto-submit"


def _ask_for_link(client, email: str):
    return client.post(
        "/login", data={"username": email, "_csrf_token": csrf_token(client, "/login")}, follow_redirects=False
    )


def _mailed_token(mail) -> str:
    return re.search(r"token=([\w-]+)", mail.text).group(1)


def test_a_link_opened_in_the_browser_that_asked_signs_in_without_a_click(client, account, sent_mail):
    _user_id, email, _central = account
    _ask_for_link(client, email)
    page = client.get(f"/login/link?token={_mailed_token(sent_mail[-1])}")
    assert AUTO in page.text and 'action="/login/link/confirm"' in page.text


def test_a_link_opened_anywhere_else_still_asks_for_the_click(client, account, sent_mail):
    """A mail scanner, or another device: it must not use the link up."""
    _user_id, email, _central = account
    _ask_for_link(client, email)
    client.cookies.clear()
    page = client.get(f"/login/link?token={_mailed_token(sent_mail[-1])}")
    assert 'action="/login/link/confirm"' in page.text and AUTO not in page.text


def test_a_link_this_browser_did_not_ask_for_needs_the_click(client, account, sent_mail):
    """Someone who sends over a link they asked for (their own account's, say)
    must not sign this browser in without a click."""
    user_id, email, _central = account
    _ask_for_link(client, email)
    elsewhere = _link(user_id, email)
    assert AUTO not in client.get(f"/login/link?token={elsewhere}").text


def test_an_unknown_address_gets_the_same_cookie(client, sent_mail):
    client.cookies.clear()
    response = _ask_for_link(client, f"nobody-{uuid.uuid4().hex[:8]}@example.com")
    assert "virgil_link_browser=" in response.headers.get("set-cookie", "")
    assert sent_mail == []
