"""Outgoing email (#168): one module sends, the rest of the app only asks it to."""

import asyncio
import logging
import smtplib

import jinja2
import pytest

from app import config
from app.i18n import current_language
from app.services import mailer


@pytest.fixture
def smtp_config(monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "smtp")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USERNAME", "no-reply@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")  # pragma: allowlist secret - synthetic fixture
    monkeypatch.setattr(config, "MAIL_FROM", "Virgil <no-reply@example.com>")


class FakeSMTP:
    """Records one session; raises on send when told to."""

    sessions: list[FakeSMTP] = []
    fail_with: Exception | None = None

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        FakeSMTP.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        if FakeSMTP.fail_with:
            raise FakeSMTP.fail_with
        self.calls.append(("send", message))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.sessions, FakeSMTP.fail_with = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _mail(**overrides) -> mailer.Mail:
    fields = {"to": "person@example.com", "subject": "Hello", "text": "secret body", "html": "<p>secret body</p>"}
    return mailer.Mail(**{**fields, **overrides})


def test_mail_is_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "")
    assert mailer.mail_enabled() is False


def test_smtp_needs_a_host_and_a_sender(monkeypatch, smtp_config):
    assert mailer.mail_enabled() is True
    monkeypatch.setattr(config, "SMTP_HOST", "")
    assert mailer.mail_enabled() is False
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "MAIL_FROM", "")
    assert mailer.mail_enabled() is False


def test_the_log_backend_counts_as_configured(monkeypatch):
    monkeypatch.setattr(config, "MAIL_BACKEND", "log")
    assert mailer.mail_enabled() is True


def test_render_mail_uses_the_recipient_language(monkeypatch):
    from app.main import templates

    loader = jinja2.DictLoader(
        {
            "email/probe.txt": 'Subject: {{ _("Signed in.") }}\n{{ _("Language changed.") }} {{ who }}\n',
            "email/probe.html": '<p>{{ _("Language changed.") }}</p>',
        }
    )
    monkeypatch.setattr(templates.env, "loader", jinja2.ChoiceLoader([loader, templates.env.loader]))
    before = current_language()

    mail = mailer.render_mail("probe", "pl", to="person@example.com", who="Ala")

    assert mail.subject == "Zalogowano."
    assert mail.text.strip() == "Zmieniono język. Ala"
    assert "Zmieniono język." in mail.html
    assert current_language() == before


def test_smtp_backend_sends_with_starttls_and_reply_to(smtp_config, fake_smtp):
    asyncio.run(mailer.send_now(_mail(reply_to="contact@example.com")))

    (session,) = fake_smtp.sessions
    assert (session.host, session.port) == ("smtp.example.com", 587)
    assert session.calls[0] == ("starttls",)
    assert session.calls[1] == ("login", "no-reply@example.com", "app-password")  # pragma: allowlist secret
    message = session.calls[2][1]
    assert message["From"] == "Virgil <no-reply@example.com>"
    assert message["To"] == "person@example.com"
    assert message["Reply-To"] == "contact@example.com"
    kinds = {part.get_content_type() for part in message.walk()}
    assert {"text/plain", "text/html"} <= kinds


def test_a_failed_send_is_logged_without_the_body(smtp_config, fake_smtp, caplog):
    fake_smtp.fail_with = smtplib.SMTPException("relay refused")

    async def scenario():
        mailer.schedule(_mail())
        await mailer.drain()

    with caplog.at_level(logging.WARNING, logger="app.services.mailer"):
        asyncio.run(scenario())

    text = caplog.text
    assert "person@example.com" in text and "SMTPException" in text
    assert "secret body" not in text


def test_the_log_backend_writes_the_whole_message_for_development(monkeypatch, caplog):
    """Development has no mailbox: the link to click is in the log. The backend
    is opted into by name, and a production instance warns about it at startup."""
    monkeypatch.setattr(config, "MAIL_BACKEND", "log")
    with caplog.at_level(logging.INFO, logger="app.services.mailer"):
        asyncio.run(mailer.send_now(_mail()))
    assert "person@example.com" in caplog.text and "Hello" in caplog.text
    assert "secret body" in caplog.text
