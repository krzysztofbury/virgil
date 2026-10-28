"""The public landing page and waitlist mode (#168)."""

import html
from urllib.parse import parse_qs, urlsplit

import pytest

from app import config

CONTACT = "contact@example.com"


@pytest.fixture
def landing(monkeypatch):
    """Landing page on, with a contact address. Tests that expect waitlist mode
    also take auth_client: before the first account exists, sign-up is open."""
    monkeypatch.setattr(config, "LANDING_PAGE", True)
    monkeypatch.setattr(config, "CONTACT_EMAIL", CONTACT)


def _hrefs(page: str) -> list[str]:
    import re

    return [html.unescape(h) for h in re.findall(r'href="([^"]+)"', page)]


def test_with_the_switch_off_slash_still_goes_to_login(client, monkeypatch):
    monkeypatch.setattr(config, "LANDING_PAGE", False)
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_a_signed_out_visitor_sees_the_landing_page_at_slash(client, auth_client, landing):
    client.cookies.clear()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 200
    page = response.text
    for text in ("Private beta", "Request access", "Works where you already talk to AI", "Shaped around you", CONTACT):
        assert text in page, text
    assert 'href="/login"' in page


def test_welcome_is_public_too(client, landing):
    client.cookies.clear()
    assert client.get("/welcome", follow_redirects=False).status_code == 200


def test_a_signed_in_visitor_still_gets_the_dashboard(auth_client, landing):
    page = auth_client.get("/", follow_redirects=False)
    assert page.status_code == 200 and "Request access" not in page.text


def test_the_request_access_links_carry_the_address_and_subject(client, auth_client, landing):
    client.cookies.clear()
    hrefs = _hrefs(client.get("/").text)
    gmail = next(h for h in hrefs if h.startswith("https://mail.google.com/"))
    assert parse_qs(urlsplit(gmail).query)["to"] == [CONTACT]
    assert parse_qs(urlsplit(gmail).query)["su"] == ["Virgil access"]
    outlook = next(h for h in hrefs if h.startswith("https://outlook.live.com/"))
    assert parse_qs(urlsplit(outlook).query)["to"] == [CONTACT]
    mailto = next(h for h in hrefs if h.startswith("mailto:"))
    assert mailto.startswith(f"mailto:{CONTACT}?") and "subject=Virgil%20access" in mailto


def test_the_landing_page_is_polish_with_the_cookie(client, auth_client, landing):
    client.cookies.clear()
    client.cookies.set("virgil_lang", "pl")
    try:
        page = client.get("/").text
        assert '<html lang="pl"' in page
        assert "Poproś o dostęp" in page
    finally:
        client.cookies.clear()


def test_open_registration_turns_the_buttons_into_create_account(client, landing, monkeypatch):
    monkeypatch.setattr(config, "REGISTRATION_OPEN", True)
    client.cookies.clear()
    page = client.get("/").text
    assert "Create account" in page and 'href="/signup"' in page
    assert "Request access" not in page


def test_signup_without_an_invite_shows_the_invite_only_message(client, auth_client, landing, monkeypatch):
    monkeypatch.setattr("app.config.REGISTRATION_OPEN", False)
    client.cookies.clear()
    page = client.get("/signup").text
    assert "Virgil is invite-only for now." in page
    assert CONTACT in page and "Request access" in page


def test_without_a_contact_address_signup_stays_as_today(client, auth_client, monkeypatch):
    monkeypatch.setattr("app.config.REGISTRATION_OPEN", False)
    monkeypatch.setattr(config, "CONTACT_EMAIL", "")
    client.cookies.clear()
    assert "Registration is closed on this server." in client.get("/signup").text
