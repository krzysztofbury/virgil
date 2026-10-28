"""Creating an account takes accepting the terms and the privacy policy, which
include consent to health data under art. 9 GDPR (#179); the time is kept."""

import sqlite3
from pathlib import Path

from conftest import _TMP, csrf_token
from fastapi.testclient import TestClient

from app.main import app

EMAIL = "consent@example.com"


def _signup(client, monkeypatch, **extra):
    monkeypatch.setattr("app.config.REGISTRATION_OPEN", True)
    browser = TestClient(app)
    browser.portal = client.portal
    password = "consent-pass-123"  # pragma: allowlist secret - synthetic fixture
    data = {"email": EMAIL, "password": password, "password_confirm": password, **extra}
    data["_csrf_token"] = csrf_token(browser, "/signup")
    return browser, browser.post("/signup", data=data, follow_redirects=False)


def _accepted_at():
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        row = central.execute("SELECT terms_accepted_at, terms_version FROM users WHERE email = ?", (EMAIL,)).fetchone()
    return row


def _forget():
    """Remove the account and its database: other tests expect one account database."""
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        row = central.execute("SELECT db_filename FROM users WHERE email = ?", (EMAIL,)).fetchone()
        central.execute("DELETE FROM users WHERE email = ?", (EMAIL,))
    if row:
        for path in (Path(_TMP) / "users").glob(f"{row[0]}*"):
            path.unlink()


def test_the_signup_page_asks_for_acceptance_with_both_documents_linked(client, auth_client, monkeypatch):
    monkeypatch.setattr("app.config.REGISTRATION_OPEN", True)
    page = TestClient(app)
    page.portal = client.portal
    html = page.get("/signup").text
    assert 'name="accept_terms"' in html and "required" in html.split('name="accept_terms"')[1].split(">")[0]
    assert 'href="/terms"' in html and 'href="/privacy"' in html


def test_without_acceptance_no_account_is_created(client, auth_client, monkeypatch):
    try:
        _browser, response = _signup(client, monkeypatch)
        assert response.status_code == 200 and "Accept the terms" in response.text
        assert _accepted_at() is None
    finally:
        _forget()


def test_acceptance_is_recorded_with_the_account(client, auth_client, monkeypatch):
    try:
        _browser, response = _signup(client, monkeypatch, accept_terms="1")
        assert response.status_code == 303
        from app.legal import LEGAL_VERSION

        accepted, version = _accepted_at()
        assert accepted and accepted.startswith("20") and version == LEGAL_VERSION
    finally:
        _forget()
