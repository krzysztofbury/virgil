"""Oura webhook endpoints for real-time data sync.

Multi-user routing: each user's subscription carries an opaque webhook_id in its
callback URL. The id resolves to a user via the central registry.

Protocol (per Oura API v2 docs/OpenAPI spec):
- Verification: Oura sends GET {callback}?verification_token=...&challenge=...
  and expects {"challenge": <challenge>} back within 10 seconds.
- Events: Oura sends POST with x-oura-signature + x-oura-timestamp headers.
  Signature = HMAC-SHA256(client_secret, timestamp + body), uppercase hex.
- Responses must arrive within 10 seconds; the actual data sync is durably queued.
"""

import hashlib
import hmac
import logging
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.central_db import get_webhook_route
from app.services.encryption import decrypt
from app.services.job_producers import WEBHOOK_TIMESTAMP_SKEW_SECONDS, enqueue_oura_sync_job, webhook_oura_job_key
from app.user_db import close_user_db, open_user_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Oura event payloads carry event_type (create/update/delete) and data_type
# (which collection changed). We sync on the data types we store.
SUPPORTED_DATA_TYPES = frozenset(
    {
        "daily_sleep",
        "daily_readiness",
        "daily_activity",
        "daily_stress",
        "sleep",
        "workout",
    }
)

_WEBHOOK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _stored_secret(raw: str) -> str:
    """Decrypt a stored webhook secret; tolerate legacy plaintext rows."""
    try:
        return decrypt(raw)
    except Exception:
        return raw


def _constant_time_eq(a: str, b: str) -> bool:
    """compare_digest on str raises TypeError for non-ASCII input — always
    compare encoded bytes (inputs here are attacker-controlled)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


async def _load_oura_integration(db) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT client_id, client_secret_enc, webhook_secret FROM integrations WHERE provider = 'oura'"
    )
    return dict(rows[0]) if rows else None


@router.post("/api/oura/webhook")
async def oura_webhook_legacy():
    """Legacy single-user endpoint — subscriptions must be re-enabled to get a
    per-user callback URL (Settings > Integrations)."""
    return Response("Webhook endpoint moved — re-enable the webhook in Settings > Integrations", status_code=410)


@router.get("/api/oura/webhook/{webhook_id}")
async def oura_webhook_verify(request: Request, webhook_id: str):
    """Subscription verification challenge (Oura sends this on subscribe)."""
    if not _WEBHOOK_ID_RE.match(webhook_id):
        return Response("Not found", status_code=404)
    user = await get_webhook_route(webhook_id)
    if not user:
        return Response("Not found", status_code=404)

    verification_token = request.query_params.get("verification_token", "")
    challenge = request.query_params.get("challenge", "")
    if not verification_token or not challenge:
        return Response("Missing verification parameters", status_code=400)

    db = await open_user_db(user["db_filename"])
    try:
        integration = await _load_oura_integration(db)
        if not integration or not integration["webhook_secret"]:
            return Response("Webhook not configured", status_code=404)
        if not _constant_time_eq(verification_token, _stored_secret(integration["webhook_secret"])):
            logger.warning("Oura webhook verification with invalid token")
            return Response("Invalid verification token", status_code=401)
        return JSONResponse({"challenge": challenge})
    finally:
        await close_user_db(db)


@router.post("/api/oura/webhook/{webhook_id}")
async def oura_webhook_event(request: Request, webhook_id: str):
    """HMAC-verified event delivery that durably coalesces sync work."""
    if not _WEBHOOK_ID_RE.match(webhook_id):
        return Response("Not found", status_code=404)
    user = await get_webhook_route(webhook_id)
    if not user:
        return Response("Not found", status_code=404)

    db = await open_user_db(user["db_filename"])
    try:
        integration = await _load_oura_integration(db)
        if not integration or not integration["webhook_secret"]:
            return Response("Webhook not configured", status_code=404)

        body = await request.body()  # raw bytes — the signature covers these

        signature = request.headers.get("x-oura-signature", "")
        timestamp = request.headers.get("x-oura-timestamp", "")
        if not signature or not timestamp:
            logger.warning("Oura webhook event missing signature/timestamp headers")
            return Response("Missing signature", status_code=403)
        try:
            delivered_at = int(timestamp)
        except (TypeError, ValueError):
            return Response("Invalid timestamp", status_code=403)
        if abs(int(time.time()) - delivered_at) > WEBHOOK_TIMESTAMP_SKEW_SECONDS:
            return Response("Stale timestamp", status_code=403)

        # Per Oura docs: HMAC-SHA256 keyed with the OAuth CLIENT SECRET over
        # timestamp + body, uppercase hex digest.
        client_secret = decrypt(integration["client_secret_enc"]) if integration["client_secret_enc"] else ""
        if not client_secret:
            return Response("Webhook not configured", status_code=404)
        expected = hmac.new(client_secret.encode(), timestamp.encode() + body, hashlib.sha256).hexdigest().upper()
        if not _constant_time_eq(signature.upper(), expected):
            logger.warning("Invalid Oura webhook signature")
            return Response("Invalid signature", status_code=403)

        # Starlette caches the body, so request.json() reuses the bytes above.
        try:
            data = await request.json()
        except Exception:
            return Response("Invalid JSON", status_code=400)
        if not isinstance(data, dict):
            return Response("Invalid JSON", status_code=400)

        data_type = data.get("data_type", "")
        if not isinstance(data_type, str) or data_type not in SUPPORTED_DATA_TYPES:
            logger.debug("Ignoring unsupported Oura data type: %r", data_type)
            return JSONResponse({"status": "ignored"})

        result = await enqueue_oura_sync_job(
            db,
            trigger="webhook",
            days_back=2,
            idempotency_key=webhook_oura_job_key(timestamp, body),
        )
        return JSONResponse({"status": "accepted" if result.created else "debounced"})
    finally:
        await close_user_db(db)
