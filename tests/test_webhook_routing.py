"""Multi-user Oura webhook routing, tested against Oura's DOCUMENTED protocol
(OpenAPI spec + webhook docs):

- Verification: GET {callback}?verification_token=...&challenge=... → {"challenge": ...}
- Events: POST with x-oura-signature + x-oura-timestamp; signature is
  HMAC-SHA256(client_secret, timestamp + body), uppercase hex.
- Legacy single-user endpoint is retired (410).
"""

import hashlib
import hmac
import json
import os
import sqlite3
import uuid

from conftest import user_db_path

VERIFICATION_TOKEN = "test-verification-token-123"
CLIENT_SECRET = "test-oura-client-secret"


def _central_conn():
    return sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])


def _seed_webhook(webhook_id: str) -> None:
    """Wire the test user up: central route + integrations row with encrypted secrets."""
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
            "VALUES (1, 'oura', 'cid', ?, ?, 'connected')",
            (encrypt(CLIENT_SECRET), encrypt(VERIFICATION_TOKEN)),
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


def _signed_headers(body: bytes, timestamp: str = "1234567890", secret: str = CLIENT_SECRET) -> dict:
    signature = hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest().upper()
    return {"x-oura-signature": signature, "x-oura-timestamp": timestamp, "content-type": "application/json"}


def test_legacy_endpoint_gone(client):
    resp = client.post("/api/oura/webhook", json={"verification_token": "x"})
    assert resp.status_code == 410


def test_malformed_webhook_id_404(client):
    assert client.post("/api/oura/webhook/not-a-valid-id", json={}).status_code == 404
    assert client.get("/api/oura/webhook/not-a-valid-id").status_code == 404


def test_unknown_webhook_id_404(client):
    assert client.post(f"/api/oura/webhook/{uuid.uuid4().hex}", json={}).status_code == 404


def test_verification_challenge_get(auth_client):
    """Oura verifies subscriptions with a GET challenge, expecting JSON back."""
    webhook_id = uuid.uuid4().hex
    _seed_webhook(webhook_id)
    try:
        url = f"/api/oura/webhook/{webhook_id}"

        resp = auth_client.get(url, params={"verification_token": VERIFICATION_TOKEN, "challenge": "abc123"})
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "abc123"}

        # Wrong token → 401; missing params → 400.
        assert auth_client.get(url, params={"verification_token": "wrong", "challenge": "abc"}).status_code == 401
        assert auth_client.get(url).status_code == 400
    finally:
        _cleanup(webhook_id)


def test_event_hmac_and_background_sync(auth_client):
    """Events: spec-correct HMAC over timestamp+body keyed with the client
    secret; the endpoint answers immediately (sync is backgrounded) and is both
    public (no session) and CSRF-exempt — no token is sent here."""
    webhook_id = uuid.uuid4().hex
    _seed_webhook(webhook_id)
    try:
        url = f"/api/oura/webhook/{webhook_id}"
        body = json.dumps({"event_type": "update", "data_type": "daily_sleep"}).encode()

        # No signature headers → 403.
        resp = auth_client.post(url, content=body, headers={"content-type": "application/json"})
        assert resp.status_code == 403

        # Signature keyed with the wrong secret → 403.
        resp = auth_client.post(url, content=body, headers=_signed_headers(body, secret="wrong-secret"))
        assert resp.status_code == 403

        # Valid signature → accepted immediately (sync runs out-of-band and
        # fails harmlessly without a real token).
        resp = auth_client.post(url, content=body, headers=_signed_headers(body))
        assert resp.status_code == 200
        assert resp.json()["status"] in ("accepted", "debounced")

        # Lowercase hex signatures are equivalent (case-insensitive compare).
        lower = {**_signed_headers(body)}
        lower["x-oura-signature"] = lower["x-oura-signature"].lower()
        resp = auth_client.post(url, content=body, headers=lower)
        assert resp.status_code == 200

        # Unsupported data type is ignored before any sync is scheduled.
        body = json.dumps({"event_type": "update", "data_type": "tag"}).encode()
        resp = auth_client.post(url, content=body, headers=_signed_headers(body))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
    finally:
        _cleanup(webhook_id)


def test_event_malformed_json_shapes_dont_500(auth_client):
    """Pre-auth surface: non-dict JSON and junk bodies must 4xx, never 500."""
    webhook_id = uuid.uuid4().hex
    _seed_webhook(webhook_id)
    try:
        url = f"/api/oura/webhook/{webhook_id}"
        for raw in (b'"just a string"', b"[1,2,3]", b"not json at all"):
            resp = auth_client.post(url, content=raw, headers=_signed_headers(raw))
            assert resp.status_code == 400, raw
    finally:
        _cleanup(webhook_id)
