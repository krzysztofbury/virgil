"""Sign-in asks for the email first, then the password or an emailed link (#168)."""

import asyncio
import re
import sqlite3
import time
import uuid
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, csrf_token

from app import config
from app.services import mailer

CHECK_EMAIL = "Check your email"
LINK_BUTTON = "Email me a sign-in link instead"


@pytest.fixture
def link_only(auth_client):
    """A link-only account (no password), with the test admin signed up first."""
    user_id = str(uuid.uuid4())
    email = f"linkonly-{user_id[:8]}@example.com"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename, language) VALUES (?, ?, '', ?, 'pl')",
        (user_id, email, f"{user_id}.db"),
    )
    central.commit()
    try:
        yield email
    finally:
        central.execute("DELETE FROM sign_in_links WHERE email = ?", (email,))
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()


@pytest.fixture(autouse=True)
def _forget_links():
    yield
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute("DELETE FROM sign_in_links")
    central.commit()
    central.close()


def _continue(client, email: str):
    return client.post("/login", data={"username": email, "_csrf_token": csrf_token(client, "/login")})


def test_the_first_step_asks_only_for_the_email(client):
    page = client.get("/login").text
    assert 'name="username"' in page and 'autocomplete="username"' in page
    assert 'type="password"' not in page


def test_a_password_account_gets_the_password_step(client, auth_client, sent_mail):
    page = _continue(client, TEST_EMAIL).text
    assert 'type="password"' in page and 'autocomplete="current-password"' in page
    assert f'value="{TEST_EMAIL}"' in page and "readonly" in page
    assert LINK_BUTTON in page
    assert sent_mail == []


def test_a_link_only_account_gets_a_link_in_its_language(client, link_only, sent_mail):
    page = _continue(client, link_only).text
    assert CHECK_EMAIL in page and 'type="password"' not in page
    (mail,) = sent_mail
    assert mail.to == link_only
    assert f"{config.BASE_URL}/login/link?token=" in mail.text
    assert "15" in mail.text
    assert mail.subject == "Twój link do logowania w Virgil"


def test_an_unknown_email_looks_like_a_link_only_one(client, link_only, sent_mail):
    known = _continue(client, link_only).text
    unknown = _continue(client, "nobody-here@example.com").text
    assert CHECK_EMAIL in unknown

    # Each response carries its own CSP nonce; apart from it and the address, the same page.
    def same(page: str, email: str) -> str:
        return re.sub(r'nonce="[^"]+"', "", page).replace(email, "X")

    assert same(unknown, "nobody-here@example.com") == same(known, link_only)
    assert [mail.to for mail in sent_mail] == [link_only]


def test_with_mail_off_every_email_gets_the_password_step(client, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    page = _continue(client, "nobody-here@example.com").text
    assert 'type="password"' in page
    assert LINK_BUTTON not in page and CHECK_EMAIL not in page


def test_email_me_a_link_instead_sends_for_a_password_account(client, auth_client, sent_mail):
    response = client.post("/login/link", data={"username": TEST_EMAIL, "_csrf_token": csrf_token(client, "/login")})
    assert CHECK_EMAIL in response.text
    assert [mail.to for mail in sent_mail] == [TEST_EMAIL]


def test_the_fourth_link_in_a_window_is_not_sent(client, link_only, sent_mail):
    pages = [_continue(client, link_only).text for _ in range(4)]
    assert all(CHECK_EMAIL in page for page in pages)
    assert len(sent_mail) == 3


def test_a_wrong_password_keeps_the_email_on_the_password_step(client, auth_client):
    response = client.post(
        "/login",
        data={"username": TEST_EMAIL, "password": "wrong-password-1", "_csrf_token": csrf_token(client, "/login")},
    )
    assert "Invalid email or password." in response.text
    assert 'type="password"' in response.text and f'value="{TEST_EMAIL}"' in response.text


def test_a_link_only_account_never_signs_in_with_a_password(client, link_only):
    response = client.post(
        "/login",
        data={"username": link_only, "password": "any-password-1", "_csrf_token": csrf_token(client, "/login")},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "Invalid email or password." in response.text


def test_the_link_request_does_not_wait_for_the_mail_server(client, link_only, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "log")

    async def slow_send(mail):
        await asyncio.sleep(2)

    monkeypatch.setattr(mailer, "send_now", slow_send)
    started = time.perf_counter()
    response = _continue(client, link_only)
    assert CHECK_EMAIL in response.text
    assert time.perf_counter() - started < 1


def test_a_link_only_address_answers_as_fast_as_an_unknown_one(client, link_only, monkeypatch):
    """Creating the link, rendering the mail and the audit event all run after
    the response, so their time cannot tell link-only accounts from unknown
    addresses (#168). A slow link store must not slow the page."""
    import app.routers.auth as auth_routes

    monkeypatch.setattr(config, "MAIL_BACKEND", "log")

    async def slow_create(*args, **kwargs):
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(auth_routes, "create_sign_in_link", slow_create)
    started = time.perf_counter()
    response = _continue(client, link_only)
    assert CHECK_EMAIL in response.text
    assert time.perf_counter() - started < 0.5
