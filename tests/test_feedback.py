"""Feedback from inside the app (#167): an email to the instance's contact
address, with the sender's account as Reply-To. Nothing is stored."""

import pytest
from conftest import TEST_EMAIL, csrf_token

from app import config

CONTACT = "contact@example.com"


@pytest.fixture
def feedback(auth_client, sent_mail, monkeypatch):
    import app.routers.settings as settings_router

    monkeypatch.setattr(config, "CONTACT_EMAIL", CONTACT)
    settings_router._feedback_sent.clear()
    return auth_client, sent_mail


def _send(client, **data):
    data.setdefault("kind", "bug")
    data.setdefault("message", "The week strip shows the wrong day.")
    data["_csrf_token"] = csrf_token(client, "/settings")
    return client.post("/feedback", data=data, follow_redirects=False)


def test_feedback_is_emailed_to_the_contact_address_with_the_sender_as_reply_to(feedback):
    client, sent = feedback
    assert 'action="/feedback"' in client.get("/settings").text
    response = _send(client, kind="idea", message="Let me export to CSV.", page="/training")
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    (mail,) = sent
    assert mail.to == CONTACT and mail.reply_to == TEST_EMAIL
    assert "Let me export to CSV." in mail.text and "/training" in mail.text and TEST_EMAIL in mail.text
    assert "idea" in mail.subject.lower()


@pytest.mark.parametrize(
    "data",
    [
        {"message": "   "},
        {"message": "x" * 4001},
        {"kind": "complaint"},
        {"page": "https://elsewhere.example/phish"},
    ],
    ids=["empty", "too-long", "unknown-kind", "foreign-page"],
)
def test_an_invalid_message_is_refused_and_nothing_is_sent(feedback, data):
    client, sent = feedback
    response = _send(client, **data)
    assert response.status_code == 303 and "err=" in response.headers["location"]
    assert sent == []


def test_feedback_is_limited_per_account(feedback):
    client, sent = feedback
    for _ in range(5):
        assert "msg=" in _send(client).headers["location"]
    assert "err=" in _send(client).headers["location"]
    assert len(sent) == 5


def test_without_mail_the_form_is_replaced_by_the_address(auth_client, monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    monkeypatch.setattr(config, "CONTACT_EMAIL", CONTACT)
    page = auth_client.get("/settings").text
    assert 'action="/feedback"' not in page and f'<a href="mailto:{CONTACT}">{CONTACT}</a>' in page
    assert "err=" in _send(auth_client).headers["location"]

    monkeypatch.setattr(config, "CONTACT_EMAIL", "")
    assert 'action="/feedback"' not in auth_client.get("/settings").text
