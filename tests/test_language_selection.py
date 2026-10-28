"""Which language a request gets (#152): the account's choice, then the cookie, then English."""

import re
import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, TEST_PASSWORD, csrf_token
from fastapi.testclient import TestClient

from app.i18n import LANGUAGE_COOKIE
from app.main import app


def _account_language() -> str:
    central = sqlite3.connect(Path(_TMP) / "central.db")
    try:
        return central.execute("SELECT language FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    finally:
        central.close()


def _set_account_language(language: str) -> None:
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute("UPDATE users SET language = ? WHERE email = ?", (language, TEST_EMAIL))
    central.commit()
    central.close()


@pytest.fixture
def anonymous(auth_client):
    """A browser with no session, on the app the shared client already started
    (auth_client makes sure the test account exists)."""
    return TestClient(app)


@pytest.fixture
def polish_account(auth_client):
    _set_account_language("pl")
    try:
        yield auth_client
    finally:
        _set_account_language("en")
        auth_client.cookies.delete(LANGUAGE_COOKIE)


def test_default_is_english(anonymous):
    response = anonymous.get("/login", headers={"Accept-Language": "pl-PL,pl;q=0.9"})
    assert '<html lang="en"' in response.text
    assert response.headers["content-language"] == "en"


def test_cookie_sets_language_before_sign_in(anonymous):
    anonymous.cookies.set(LANGUAGE_COOKIE, "pl")
    response = anonymous.get("/login")
    assert '<html lang="pl"' in response.text
    assert response.headers["content-language"] == "pl"


@pytest.mark.parametrize("value", ["de", "", "x" * 5000], ids=["other", "empty", "huge"])
def test_garbage_values_mean_english(anonymous, value):
    anonymous.cookies.set(LANGUAGE_COOKIE, value)
    assert '<html lang="en"' in anonymous.get("/login").text

    token = csrf_token(anonymous, "/login")
    response = anonymous.post(
        "/language", data={"language": value, "next": "/login", "_csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.cookies.get(LANGUAGE_COOKIE) == "en"


def test_account_choice_wins_over_cookie(polish_account):
    polish_account.cookies.set(LANGUAGE_COOKIE, "en")
    assert '<html lang="pl"' in polish_account.get("/daily").text


def test_switch_while_signed_in_updates_account_and_cookie(auth_client):
    try:
        token = csrf_token(auth_client, "/daily")
        response = auth_client.post(
            "/language", data={"language": "pl", "next": "/daily", "_csrf_token": token}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/daily?")
        assert response.cookies.get(LANGUAGE_COOKIE) == "pl"
        assert _account_language() == "pl"
        assert '<html lang="pl"' in auth_client.get("/daily").text
    finally:
        _set_account_language("en")
        auth_client.cookies.delete(LANGUAGE_COOKIE)


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example", "daily"])
def test_switch_redirects_only_locally(anonymous, target):
    token = csrf_token(anonymous, "/login")
    response = anonymous.post(
        "/language", data={"language": "pl", "next": target, "_csrf_token": token}, follow_redirects=False
    )
    assert response.headers["location"].split("?")[0] == "/"


def test_sign_in_sets_cookie_to_account_language(anonymous):
    _set_account_language("pl")
    try:
        token = csrf_token(anonymous, "/login")
        response = anonymous.post(
            "/login",
            data={"username": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.cookies.get(LANGUAGE_COOKIE) == "pl"
    finally:
        _set_account_language("en")


def test_signup_keeps_the_language_chosen_before_it(anonymous, monkeypatch):

    monkeypatch.setattr("app.config.REGISTRATION_OPEN", True)
    anonymous.cookies.set(LANGUAGE_COOKIE, "pl")
    email = "polish-signup@example.com"
    password = "polish-signup-password-1"  # pragma: allowlist secret
    token = csrf_token(anonymous, "/signup")
    response = anonymous.post(
        "/signup",
        data={"email": email, "password": password, "password_confirm": password, "_csrf_token": token},
        follow_redirects=False,
    )
    central = sqlite3.connect(Path(_TMP) / "central.db")
    try:
        row = central.execute("SELECT id, language, db_filename FROM users WHERE email = ?", (email,)).fetchone()
        assert response.status_code == 303, response.text
        assert row[1] == "pl"
    finally:
        if row:
            central.execute("DELETE FROM users WHERE id = ?", (row[0],))
            central.commit()
            for suffix in ("", "-wal", "-shm"):
                Path(_TMP, "users", row[2] + suffix).unlink(missing_ok=True)
        central.close()


@pytest.mark.parametrize("path", ["/login", "/signup", "/reset?token=unknown"])
def test_the_switch_is_on_the_pages_before_sign_in(anonymous, path):
    page = anonymous.get(path).text
    assert 'action="/language"' in page
    assert 'name="language" value="pl"' in page and 'name="language" value="en"' in page


def test_settings_offers_the_language_with_the_current_one_selected(auth_client):
    page = auth_client.get("/settings?tab=general").text
    assert 'action="/language"' in page
    pressed = dict(re.findall(r'name="language" value="(\w+)".*?aria-pressed="(\w+)"', page, re.S))
    assert pressed == {"en": "true", "pl": "false"}, "one click on a language saves it; the current one is marked"


def test_a_choice_made_before_sign_in_is_kept_by_the_account(anonymous):
    """Choosing Polski on the sign-in page, then signing in to an account still on
    English, keeps Polski: it is the latest thing this person asked for."""
    token = csrf_token(anonymous, "/login")
    anonymous.post("/language", data={"language": "pl", "next": "/login", "_csrf_token": token})
    try:
        token = csrf_token(anonymous, "/login")
        response = anonymous.post(
            "/login",
            data={"username": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert _account_language() == "pl"
        assert response.cookies.get(LANGUAGE_COOKIE) == "pl"
    finally:
        _set_account_language("en")


def test_a_cookie_left_by_an_earlier_sign_in_does_not_override_the_account(anonymous):
    """A cookie set at an earlier sign-in is not a choice: another device may have
    changed the account's language since."""
    _set_account_language("pl")
    anonymous.cookies.set(LANGUAGE_COOKIE, "en")
    try:
        token = csrf_token(anonymous, "/login")
        response = anonymous.post(
            "/login",
            data={"username": TEST_EMAIL, "password": TEST_PASSWORD, "_csrf_token": token},
            follow_redirects=False,
        )
        assert _account_language() == "pl"
        assert response.cookies.get(LANGUAGE_COOKIE) == "pl"
    finally:
        _set_account_language("en")
