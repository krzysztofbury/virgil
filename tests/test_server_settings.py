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


@pytest.mark.parametrize(
    ("address", "credential"), [("SMTP_HOST", "SMTP_PASSWORD"), ("OFFSITE_ENDPOINT", "OFFSITE_SECRET_ACCESS_KEY")]
)
def test_a_new_address_does_not_inherit_the_old_credential(settings, monkeypatch, address, credential):
    """Otherwise pointing the host elsewhere would hand the stored password to it."""
    monkeypatch.setattr(config, address, "https://old.example" if address == "OFFSITE_ENDPOINT" else "old.example")
    monkeypatch.setattr(config, credential, "env-credential")
    new = "https://new.example" if address == "OFFSITE_ENDPOINT" else "new.example"

    settings(server_settings.save, address, new)
    assert getattr(current, credential) == ""

    settings(server_settings.save, credential, "entered-again")
    settings(server_settings.save, address, new)
    assert getattr(current, credential) == "entered-again", "saving the same address again keeps it"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/js/script.js",
        "https://plausible.io.evil.example/js/x.js",
        "https://plausible.io/js/x.js; script-src *",
        "https://user@plausible.io/js/x.js",
    ],
)
def test_the_analytics_script_is_plausibles_own(settings, url):
    with pytest.raises(SettingError):
        settings(server_settings.save, "PLAUSIBLE_SCRIPT_URL", url)


def test_a_malformed_environment_script_url_never_reaches_the_policy(monkeypatch):
    from app.security_headers import content_security_policy

    monkeypatch.setattr(config, "PLAUSIBLE_SCRIPT_URL", "https://a.example; script-src *")
    assert "script-src *" not in content_security_policy("n")


def test_a_credential_cannot_fall_back_to_the_environment_for_a_new_address(settings, monkeypatch):
    """Reset after a new host would hand the environment's password to that host."""
    monkeypatch.setattr(config, "SMTP_HOST", "old.example")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "env-credential")
    settings(server_settings.save, "SMTP_HOST", "new.example")
    with pytest.raises(SettingError):
        settings(server_settings.reset, "SMTP_PASSWORD")
    assert current.SMTP_PASSWORD == ""

    settings(server_settings.reset, "SMTP_HOST")
    settings(server_settings.reset, "SMTP_PASSWORD")
    assert current.SMTP_PASSWORD == "env-credential"  # pragma: allowlist secret


@pytest.mark.parametrize(
    ("address", "credential"), [("SMTP_HOST", "SMTP_PASSWORD"), ("OFFSITE_ENDPOINT", "OFFSITE_SECRET_ACCESS_KEY")]
)
def test_resetting_the_address_drops_the_credential_saved_for_the_other_one(settings, monkeypatch, address, credential):
    env_address = "https://env.example" if address == "OFFSITE_ENDPOINT" else "env.example"
    monkeypatch.setattr(config, address, env_address)
    monkeypatch.setattr(config, credential, "env-credential")  # pragma: allowlist secret
    settings(server_settings.save, address, "https://b.example" if address == "OFFSITE_ENDPOINT" else "b.example")
    settings(server_settings.save, credential, "credential-for-b")  # pragma: allowlist secret
    settings(server_settings.reset, address)
    assert getattr(current, address) == env_address
    assert getattr(current, credential) == "env-credential"  # pragma: allowlist secret


def test_an_undecryptable_secret_is_skipped_not_fatal(settings, caplog, monkeypatch):
    monkeypatch.setattr(config, "SMTP_PASSWORD", "env-credential")  # pragma: allowlist secret
    settings(server_settings.save, "SMTP_PASSWORD", "readable-secret")  # pragma: allowlist secret
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        central.execute("UPDATE server_settings SET value = 'not-a-fernet-token' WHERE name = 'SMTP_PASSWORD'")
    settings(server_settings.load)
    # Empty, not the environment's: that one belongs to the environment's host,
    # and the host in use may be one saved here.
    assert current.SMTP_PASSWORD == ""
    assert "SMTP_PASSWORD" in caplog.text


def test_the_whole_plausible_snippet_is_accepted_and_reduced_to_its_script(settings):
    snippet = (
        "<!-- Privacy-friendly analytics by Plausible -->\n"
        '<script async src="https://plausible.io/js/pa-abc_DEF123.js"></script>\n'
        "<script>window.plausible=window.plausible||function(){};plausible.init()</script>"
    )
    settings(server_settings.save, "PLAUSIBLE_SCRIPT_URL", snippet)
    assert current.PLAUSIBLE_SCRIPT_URL == "https://plausible.io/js/pa-abc_DEF123.js"


def test_an_off_site_value_cleared_in_the_app_is_reported_as_the_apps(settings, monkeypatch):
    from pyrage import x25519

    from app.services import offsite

    for name, value in {
        "OFFSITE_ENDPOINT": "https://env.r2.example",
        "OFFSITE_BUCKET": "env-bucket",
        "OFFSITE_ACCESS_KEY_ID": "env-id",
        "OFFSITE_SECRET_ACCESS_KEY": "env-secret",  # pragma: allowlist secret
        "OFFSITE_RECIPIENT": str(x25519.Identity.generate().to_public()),
    }.items():
        monkeypatch.setattr(config, name, value)
    settings(server_settings.save, "OFFSITE_BUCKET", "")
    with pytest.raises(offsite.OffsiteConfigError, match=r"VIRGIL_OFFSITE_BUCKET \(empty in Settings > Server\)"):
        offsite.offsite_settings()
