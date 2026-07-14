"""Oura webhook endpoints for real-time data sync.

Multi-user routing: each user's subscription carries an opaque webhook_id in its
callback URL. The id resolves to a user via the central registry.

Protocol (per Oura API v2 docs/OpenAPI spec):
- Verification: Oura sends GET {callback}?verification_token=...&challenge=...
  and expects {"challenge": <challenge>} back within 10 seconds.
- Events: Oura sends POST with x-oura-signature + x-oura-timestamp headers.
  Signature = HMAC-SHA256(client_secret, timestamp + body), uppercase hex.
- Responses must arrive within 10 seconds — the actual data sync therefore runs
  as a debounced background task, never inline.
"""

import asyncio
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

# Debounce: at most one in-flight sync per user DB. Oura retries up to 10x and
# we hold 12 subscriptions, so bursts of events for the same user are the
# normal case. Membership in _pending is checked-and-set synchronously on the
# event loop — a lock's .locked() probe raced: N deliveries could all pass it
# before any task started, then run N sequential full syncs.
_pending_syncs: set[str] = set()
_background_tasks: set[asyncio.Task] = set()


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


def _schedule_user_sync(db_filename: str, data_type: str) -> bool:
    """Run a 2-day sync in the background, at most one at a time per user.

    Returns False when a sync is already pending/running (event debounced).
    The check-and-set below runs synchronously on the event loop, so no other
    coroutine can interleave between the membership test and the add.
    """
    if db_filename in _pending_syncs:
        return False

    async def _run() -> None:
        # One blanket except: an open_user_db failure escaping the coroutine
        # would only surface as "Task exception was never retrieved".
        try:
            db = await open_user_db(db_filename)
            try:
                from app.services.oura_api import sync_oura_from_api

                count = await sync_oura_from_api(db, days_back=2)
                logger.info("Oura webhook sync completed: %d days (data_type: %s)", count, data_type)
            finally:
                await close_user_db(db)
        except Exception:
            logger.exception("Oura webhook sync failed for data_type: %s", data_type)
        finally:
            _pending_syncs.discard(db_filename)

    task = asyncio.create_task(_run())
    # Registered AFTER create_task (the coroutine body only starts on the next
    # loop iteration, so this is still atomic) — adding before would leak the
    # entry forever if create_task itself raised, permanently debouncing the user.
    _pending_syncs.add(db_filename)
    # Keep a strong reference so the task isn't garbage-collected mid-flight.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return True


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
    """Data event delivery — HMAC-verified, sync runs in the background."""
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

        # Respond inside Oura's 10s deadline — sync happens out-of-band.
        scheduled = _schedule_user_sync(user["db_filename"], data_type)
        return JSONResponse({"status": "accepted" if scheduled else "debounced"})
    finally:
        await close_user_db(db)
