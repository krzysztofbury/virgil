"""HTTP-level Oura subscription enable and disable lifecycle."""

import os
import sqlite3

from conftest import csrf_token, user_db_path


def test_settings_enable_and_disable_oura_subscriptions(auth_client, monkeypatch):
    import app.routers.settings as settings_router
    import app.services.oura_subscriptions as oura_subscriptions
    from app.services.encryption import encrypt

    remote = []
    deleted = []

    async def valid_token(_db):
        return "access-token"

    async def list_remote(_client_id, _client_secret):
        return list(remote)

    async def create_remote(_client_id, _client_secret, callback_url, _token, event_type, data_type):
        payload = {
            "id": f"{event_type}-{data_type}",
            "callback_url": callback_url,
            "event_type": event_type,
            "data_type": data_type,
            "expiration_time": "2027-09-01T12:00:00Z",
        }
        remote.append(payload)
        return payload

    async def delete_remote(_client_id, _client_secret, remote_id):
        deleted.append(remote_id)
        remote[:] = [item for item in remote if item["id"] != remote_id]

    monkeypatch.setattr(settings_router, "ensure_valid_token", valid_token)
    monkeypatch.setattr(oura_subscriptions, "list_oura_webhook_subscriptions", list_remote)
    monkeypatch.setattr(oura_subscriptions, "create_oura_webhook_subscription", create_remote)
    monkeypatch.setattr(oura_subscriptions, "delete_oura_webhook_subscription", delete_remote)

    user_db = sqlite3.connect(user_db_path())
    central_db = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
    try:
        user_db.execute("DELETE FROM integrations WHERE provider = 'oura'")
        user_db.execute(
            """INSERT INTO integrations
               (provider, client_id, client_secret_enc, access_token_enc, status, scopes, webhook_secret)
               VALUES ('oura', 'client-id', ?, ?, 'connected', 'daily sleep workout', '')""",
            (encrypt("client-secret"), encrypt("access-token")),
        )
        user_db.commit()

        token = csrf_token(auth_client, "/settings?tab=integrations")
        enabled = auth_client.post(
            "/settings/oura/webhook/enable",
            data={"_csrf_token": token},
            follow_redirects=False,
        )
        assert enabled.status_code == 303
        assert "msg=Oura+webhook+enabled" in enabled.headers["location"]
        assert len(remote) == 12

        registration = central_db.execute(
            """SELECT status, desired_state, renewal_due_at
               FROM provider_subscription_registrations WHERE provider = 'oura'"""
        ).fetchone()
        assert registration is not None
        assert registration[0:2] == ("active", "enabled")
        assert registration[2]
        assert "Active" in auth_client.get("/settings?tab=integrations").text

        token = csrf_token(auth_client, "/settings?tab=integrations")
        disabled = auth_client.post(
            "/settings/oura/webhook/disable",
            data={"_csrf_token": token},
            follow_redirects=False,
        )
        assert disabled.status_code == 303
        assert "msg=Oura+webhook+disabled" in disabled.headers["location"]
        assert len(deleted) == 12
        assert remote == []
        assert (
            central_db.execute("SELECT 1 FROM provider_subscription_registrations WHERE provider = 'oura'").fetchone()
            is None
        )
        secret = user_db.execute("SELECT webhook_secret FROM integrations WHERE provider = 'oura'").fetchone()[0]
        assert secret == ""
    finally:
        central_db.execute("DELETE FROM provider_subscription_items WHERE provider = 'oura'")
        central_db.execute("DELETE FROM provider_subscription_registrations WHERE provider = 'oura'")
        central_db.execute("DELETE FROM webhook_routes WHERE provider = 'oura'")
        central_db.commit()
        central_db.close()
        user_db.execute("DELETE FROM integrations WHERE provider = 'oura'")
        user_db.commit()
        user_db.close()
