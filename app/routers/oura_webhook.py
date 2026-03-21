"""Oura webhook endpoint for real-time data sync."""

import hashlib
import hmac
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

SUPPORTED_EVENT_TYPES = frozenset(
    {
        "daily_sleep",
        "daily_readiness",
        "daily_activity",
        "daily_stress",
        "sleep",
        "workout",
    }
)


@router.post("/api/oura/webhook")
async def oura_webhook(request: Request):
    db = await get_db()

    # Get stored webhook secret
    row = await db.execute_fetchall("SELECT webhook_secret FROM integrations WHERE provider = 'oura'")
    if not row or not row[0]["webhook_secret"]:
        return Response("Webhook not configured", status_code=404)

    secret = row[0]["webhook_secret"]
    body = await request.body()  # Cache raw bytes for HMAC verification

    # Subscription verification: Oura sends verification_token in JSON
    # Starlette caches the body, so request.json() reuses the bytes above
    try:
        data = await request.json()
    except Exception:
        return Response("Invalid JSON", status_code=400)

    # Handle verification challenge
    if "verification_token" in data:
        if data["verification_token"] == secret:
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

    # Process event
    event_type = data.get("event_type", "")
    if event_type not in SUPPORTED_EVENT_TYPES:
        logger.debug("Ignoring unsupported Oura event type: %s", event_type)
        return JSONResponse({"status": "ignored"})

    # Trigger sync for the event date range
    try:
        from app.services.oura_api import sync_oura_from_api

        count = await sync_oura_from_api(db, days_back=2)
        logger.info("Oura webhook sync completed: %d days (event: %s)", count, event_type)
    except Exception:
        logger.exception("Oura webhook sync failed for event: %s", event_type)

    return JSONResponse({"status": "ok"})
