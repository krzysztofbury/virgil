"""An invite made for an address is emailed to it (#168)."""

import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP, csrf_token

from app import config


@pytest.fixture(autouse=True)
def _drop_invites():
    yield
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute("DELETE FROM invites WHERE used_at = ''")
    central.commit()
    central.close()


def _invite(auth_client, email: str, language: str = "en"):
    return auth_client.post(
        "/admin/invites",
        data={
            "email": email,
            "role": "user",
            "language": language,
            "_csrf_token": csrf_token(auth_client, "/admin/users"),
        },
    )


def test_an_invite_with_an_address_is_emailed_in_the_chosen_language(auth_client, sent_mail):
    page = _invite(auth_client, "guest@example.com", "pl")
    (mail,) = sent_mail
    assert mail.to == "guest@example.com"
    assert mail.subject == "Zaproszenie do Virgila"
    assert f"{config.BASE_URL}/signup?invite=" in mail.text
    assert "Sent to guest@example.com." in page.text
    assert "/signup?invite=" in page.text, "the link is still shown once"


def test_an_invite_without_an_address_is_not_emailed(auth_client, sent_mail):
    page = _invite(auth_client, "")
    assert sent_mail == []
    assert "/signup?invite=" in page.text


def test_with_mail_off_the_admin_hands_the_link_over_as_today(auth_client, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    page = _invite(auth_client, "guest@example.com")
    assert "Sent to guest@example.com." not in page.text
    assert "Send this link to guest@example.com." in page.text


def test_the_invite_form_offers_both_languages(auth_client, sent_mail):
    page = auth_client.get("/admin/users").text
    assert 'name="language"' in page and 'value="pl"' in page and 'value="en"' in page
