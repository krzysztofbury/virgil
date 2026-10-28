"""Server settings an admin changes in the app (#179): a saved value overrides
the environment until it is reset, and secrets are stored encrypted."""

import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP

from app import config, server_settings
from app.server_settings import SettingError, current


@pytest.fixture
def settings(client):
    """Run the module's coroutines on the app's loop, and leave no override behind."""
    call = client.portal.call
    yield call
    for setting in server_settings.SETTINGS:
        call(server_settings.reset, setting.attribute)


def _stored(attribute: str) -> str | None:
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        row = central.execute("SELECT value FROM server_settings WHERE name = ?", (attribute,)).fetchone()
    return row[0] if row else None


def test_a_saved_value_overrides_the_environment_until_it_is_reset(settings, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.from-env.example")
    assert current.SMTP_HOST == "smtp.from-env.example"
    assert server_settings.source("SMTP_HOST") == "environment"

    settings(server_settings.save, "SMTP_HOST", "smtp.from-app.example")
    assert current.SMTP_HOST == "smtp.from-app.example"
    assert server_settings.source("SMTP_HOST") == "app"

    settings(server_settings.reset, "SMTP_HOST")
    assert current.SMTP_HOST == "smtp.from-env.example"
    assert _stored("SMTP_HOST") is None


def test_values_come_back_typed_like_the_environment_ones(settings):
    settings(server_settings.save, "SMTP_PORT", "465")
    settings(server_settings.save, "REGISTRATION_OPEN", "true")
    assert current.SMTP_PORT == 465
    assert current.REGISTRATION_OPEN is True
    settings(server_settings.save, "REGISTRATION_OPEN", "false")
    assert current.REGISTRATION_OPEN is False


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("SMTP_PORT", "0"),
        ("SMTP_PORT", "smtp"),
        ("REGISTRATION_OPEN", "yes"),
        ("CONTACT_EMAIL", "not-an-address"),
        ("MAIL_FROM", "Virgil <no-at-sign>"),
        ("ALERT_WEBHOOK_URL", "http://hooks.example/plain-http"),
        ("OFFSITE_ENDPOINT", "ftp://r2.example"),
        ("OFFSITE_RECIPIENT", "age1-not-a-key"),
        ("MAIL_BACKEND", "log"),
    ],
)
def test_an_invalid_value_is_refused_and_changes_nothing(settings, attribute, value):
    before = getattr(current, attribute)
    with pytest.raises(SettingError):
        settings(server_settings.save, attribute, value)
    assert getattr(current, attribute) == before
    assert _stored(attribute) is None


def test_a_secret_is_encrypted_at_rest_and_survives_a_reload(settings):
    settings(server_settings.save, "SMTP_PASSWORD", "hunter2-smtp")  # pragma: allowlist secret
    stored = _stored("SMTP_PASSWORD")
    assert stored and "hunter2" not in stored

    server_settings._values.clear()
    settings(server_settings.load)
    assert current.SMTP_PASSWORD == "hunter2-smtp"  # pragma: allowlist secret
    assert server_settings.masked("SMTP_PASSWORD") == "...smtp"


def test_an_unknown_setting_cannot_be_saved(settings):
    for attribute in ("ENCRYPTION_KEY", "ADMIN_EMAILS", "REQUIRE_TOTP", "BASE_URL"):
        with pytest.raises(SettingError):
            settings(server_settings.save, attribute, "x")


def test_off_site_fields_merge_one_by_one_before_the_half_set_check(settings, monkeypatch):
    """Two fields saved in the app, three from the environment: one destination."""
    from pyrage import x25519

    from app.services import offsite

    identity = x25519.Identity.generate()
    for name, value in {
        "OFFSITE_ENDPOINT": "https://env.r2.example",
        "OFFSITE_BUCKET": "env-bucket",
        "OFFSITE_ACCESS_KEY_ID": "env-id",
        "OFFSITE_SECRET_ACCESS_KEY": "env-secret",  # pragma: allowlist secret
        "OFFSITE_RECIPIENT": str(identity.to_public()),
    }.items():
        monkeypatch.setattr(config, name, value)
    settings(server_settings.save, "OFFSITE_BUCKET", "app-bucket")
    settings(server_settings.save, "OFFSITE_SECRET_ACCESS_KEY", "app-secret")  # pragma: allowlist secret

    destination = offsite.offsite_settings()
    assert (destination.endpoint, destination.bucket, destination.access_key_id) == (
        "https://env.r2.example",
        "app-bucket",
        "env-id",
    )
    assert destination.secret_access_key == "app-secret"  # pragma: allowlist secret

    settings(server_settings.save, "OFFSITE_ENDPOINT", "")
    with pytest.raises(offsite.OffsiteConfigError, match="VIRGIL_OFFSITE_ENDPOINT"):
        offsite.offsite_settings()
