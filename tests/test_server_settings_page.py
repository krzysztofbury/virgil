"""Settings > Users and Settings > Server, the admin's part of Settings (#179)."""

import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, csrf_token

from app import server_settings


@pytest.fixture
def admin(auth_client, client, monkeypatch):
    """The admin's client; alerts are captured, and no saved setting outlives the test."""
    sent = []

    async def capture(url, text):
        sent.append((url, text))
        return True

    monkeypatch.setattr("app.alerts.send_alert", capture)
    yield auth_client, sent
    for setting in server_settings.SETTINGS:
        client.portal.call(server_settings.reset, setting.attribute)
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        central.execute("DELETE FROM server_llm_providers")


def _post(client, path: str, **data):
    data["_csrf_token"] = csrf_token(client, "/settings?tab=server")
    return client.post(path, data=data, follow_redirects=False)


def _role(role: str) -> None:
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        central.execute("UPDATE users SET role = ? WHERE email = ?", (role, TEST_EMAIL))


def test_the_admin_panel_lives_in_settings(admin):
    client, _sent = admin
    page = client.get("/settings").text
    assert 'href="/settings?tab=users"' in page and 'href="/settings?tab=server"' in page
    moved = client.get("/admin/users", follow_redirects=False)
    assert (moved.status_code, moved.headers["location"]) == (303, "/settings?tab=users")
    assert "Security events" in client.get("/settings?tab=users").text


def test_an_account_that_is_not_an_admin_sees_and_changes_nothing(admin):
    client, sent = admin
    token = csrf_token(client, "/settings")
    _role("user")
    try:
        page = client.get("/settings").text
        assert "tab=server" not in page and "tab=users" not in page
        assert client.get("/settings?tab=server").status_code == 403
        refused = client.post("/admin/server/settings/SMTP_HOST", data={"value": "evil.example", "_csrf_token": token})
        assert refused.status_code == 403
    finally:
        _role("admin")
    assert server_settings.source("SMTP_HOST") != "app"
    assert sent == []


def test_a_saved_secret_is_never_shown_back_and_the_change_is_recorded(admin):
    client, sent = admin
    _post(client, "/admin/server/settings/ALERT_WEBHOOK_URL", value="https://hooks.example/old-hook-abcd")
    sent.clear()

    response = _post(client, "/admin/server/settings/SMTP_PASSWORD", value="hunter2-smtp-wxyz")
    assert response.status_code == 303

    page = client.get("/settings?tab=server").text
    assert "hunter2" not in page and "...wxyz" in page
    assert "old-hook" not in page and "...abcd" in page
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        detail = central.execute(
            "SELECT detail FROM auth_events WHERE event = 'server_setting' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert "VIRGIL_SMTP_PASSWORD" in detail and "hunter2" not in detail
    assert sent == [("https://hooks.example/old-hook-abcd", f"Virgil: {TEST_EMAIL} changed VIRGIL_SMTP_PASSWORD.")]


def test_moving_the_alert_webhook_tells_the_old_one(admin):
    client, sent = admin
    _post(client, "/admin/server/settings/ALERT_WEBHOOK_URL", value="https://hooks.example/first")
    sent.clear()
    _post(client, "/admin/server/settings/ALERT_WEBHOOK_URL", value="https://attacker.example/second")
    assert [url for url, _text in sent] == ["https://hooks.example/first"]


def test_an_invalid_value_is_refused_with_a_message(admin):
    client, _sent = admin
    response = _post(client, "/admin/server/settings/SMTP_PORT", value="smtp")
    assert response.status_code == 303 and "err=" in response.headers["location"]
    assert server_settings.source("SMTP_PORT") != "app"


def test_reset_returns_to_the_environment(admin):
    client, _sent = admin
    _post(client, "/admin/server/settings/CONTACT_EMAIL", value="hello@example.com")
    assert server_settings.source("CONTACT_EMAIL") == "app"
    _post(client, "/admin/server/settings/CONTACT_EMAIL/reset")
    assert server_settings.source("CONTACT_EMAIL") != "app"


def test_the_server_ai_key_is_added_and_put_in_use_from_the_page(admin):
    client, _sent = admin
    _post(client, "/admin/server/llm/add", provider="xai", model="xai/grok-4.7", api_key="xai-key-0000-9876")
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        (provider_id,) = central.execute("SELECT id FROM server_llm_providers").fetchone()
    _post(client, "/admin/server/llm/activate", provider_id=str(provider_id))
    page = client.get("/settings?tab=server").text
    assert "xai/grok-4.7" in page and "...9876" in page and "xai-key-0000" not in page
