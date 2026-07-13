"""Multi-user Oura webhook routing: per-user callback URLs, HMAC against the
user's own (encrypted) secret, legacy endpoint retired."""

import hashlib
import hmac
import json
import os
import sqlite3
import uuid

from conftest import user_db_path

SECRET = "test-verification-token-123"


def _central_conn():
    return sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])


def _seed_webhook(webhook_id: str) -> None:
    """Wire the test user up: central route + integrations row with encrypted secret."""
    from app.services.encryption import encrypt

    central = _central_conn()
    try:
        user_id = central.execute("SELECT id FROM users LIMIT 1").fetchone()[0]
        central.execute(
            "INSERT OR REPLACE INTO webhook_routes (webhook_id, user_id, provider) VALUES (?, ?, 'oura')",
            (webhook_id, user_id),
        )
        central.commit()
    finally:
        central.close()

    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "INSERT OR REPLACE INTO integrations "
            "(id, provider, client_id, client_secret_enc, webhook_secret, status) "
            "VALUES (1, 'oura', 'cid', '', ?, 'connected')",
            (encrypt(SECRET),),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup(webhook_id: str) -> None:
    central = _central_conn()
    try:
        central.execute("DELETE FROM webhook_routes WHERE webhook_id = ?", (webhook_id,))
        central.commit()
    finally:
        central.close()
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM integrations WHERE provider = 'oura'")
        conn.commit()
    finally:
        conn.close()


def test_legacy_endpoint_gone(client):
    resp = client.post("/api/oura/webhook", json={"verification_token": "x"})
    assert resp.status_code == 410


def test_malformed_webhook_id_404(client):
    resp = client.post("/api/oura/webhook/not-a-valid-id", json={})
    assert resp.status_code == 404


def test_unknown_webhook_id_404(client):
    resp = client.post(f"/api/oura/webhook/{uuid.uuid4().hex}", json={})
    assert resp.status_code == 404


def test_verification_challenge_and_hmac(auth_client):
    """Full flow: challenge echo, bad signature rejected, good signature accepted.

    Also proves the endpoint is public (no session cookie needed) and CSRF-exempt —
    posts carry no CSRF token yet are not 403'd.
    """
    webhook_id = uuid.uuid4().hex
    _seed_webhook(webhook_id)
    try:
        url = f"/api/oura/webhook/{webhook_id}"

        # Verification challenge echoes the secret.
        resp = auth_client.post(url, json={"verification_token": SECRET})
        assert resp.status_code == 200
        assert resp.text == SECRET

        # Wrong verification token → 403.
        resp = auth_client.post(url, json={"verification_token": "wrong"})
        assert resp.status_code == 403

        # Event without signature → 403.
        resp = auth_client.post(url, json={"event_type": "update", "data_type": "daily_sleep"})
        assert resp.status_code == 403

        # Valid HMAC over the raw body → accepted (sync fails without a real
        # Oura token, but the handler absorbs that and still returns ok).
        body = json.dumps({"event_type": "update", "data_type": "daily_sleep"}).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        resp = auth_client.post(
            url, content=body, headers={"x-oura-signature": sig, "content-type": "application/json"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        # Unsupported data type is ignored, not synced.
        body = json.dumps({"event_type": "update", "data_type": "tag"}).encode()
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        resp = auth_client.post(
            url, content=body, headers={"x-oura-signature": sig, "content-type": "application/json"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
    finally:
        _cleanup(webhook_id)
