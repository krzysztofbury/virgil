"""Oura webhook endpoint for real-time data sync.

Multi-user routing: each user's subscription carries an opaque webhook_id in its
callback URL. The id resolves to a user via the central registry, and all
verification (challenge token, HMAC signature) runs against that user's own
encrypted secret — cookies play no part here.
"""

import hashlib
import hmac
import logging
import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.central_db import get_webhook_route
from app.services.encryption import decrypt
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


@router.post("/api/oura/webhook")
async def oura_webhook_legacy():
    """Legacy single-user endpoint — subscriptions must be re-enabled to get a
    per-user callback URL (Settings > Integrations)."""
    return Response("Webhook endpoint moved — re-enable the webhook in Settings > Integrations", status_code=410)


@router.post("/api/oura/webhook/{webhook_id}")
async def oura_webhook(request: Request, webhook_id: str):
    if not _WEBHOOK_ID_RE.match(webhook_id):
        return Response("Not found", status_code=404)

    user = await get_webhook_route(webhook_id)
    if not user:
        return Response("Not found", status_code=404)

    db = await open_user_db(user["db_filename"])
    try:
        row = await db.execute_fetchall("SELECT webhook_secret FROM integrations WHERE provider = 'oura'")
        if not row or not row[0]["webhook_secret"]:
            return Response("Webhook not configured", status_code=404)

        secret = _stored_secret(row[0]["webhook_secret"])
        body = await request.body()  # Cache raw bytes for HMAC verification

        # Starlette caches the body, so request.json() reuses the bytes above
        try:
            data = await request.json()
        except Exception:
            return Response("Invalid JSON", status_code=400)

        # Handle subscription verification challenge
        if "verification_token" in data:
            if hmac.compare_digest(data["verification_token"], secret):
                return Response(secret, media_type="text/plain")
            return Response("Invalid verification token", status_code=403)

        # Require and validate HMAC-SHA256 signature on all non-verification requests
        signature = request.headers.get("x-oura-signature", "")
        if not signature:
            logger.warning("Oura webhook request missing signature header")
            return Response("Missing signature", status_code=403)
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            logger.warning("Invalid Oura webhook signature")
            return Response("Invalid signature", status_code=403)

        # Process event — payload has event_type (create/update/delete) + data_type.
        data_type = data.get("data_type", "") or data.get("event_type", "")
        if data_type not in SUPPORTED_DATA_TYPES:
            logger.debug("Ignoring unsupported Oura data type: %s", data_type)
            return JSONResponse({"status": "ignored"})

        # Trigger sync for the event date range
        try:
            from app.services.oura_api import sync_oura_from_api

            count = await sync_oura_from_api(db, days_back=2)
            logger.info("Oura webhook sync completed: %d days (data_type: %s)", count, data_type)
        except Exception:
            logger.exception("Oura webhook sync failed for data_type: %s", data_type)

        return JSONResponse({"status": "ok"})
    finally:
        await close_user_db(db)
