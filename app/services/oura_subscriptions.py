"""Oura adapter for the provider-neutral subscription lifecycle."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from app.central_db import delete_webhook_routes, get_webhook_id_for_user, get_webhook_ids_for_user
from app.config import BASE_URL
from app.db import set_setting
from app.services.encryption import decrypt
from app.services.oura_api import (
    create_oura_webhook_subscription,
    delete_oura_webhook_subscription,
    list_oura_webhook_subscriptions,
    renew_oura_webhook_subscription,
)
from app.services.subscriptions import (
    RemoteSubscription,
    SubscriptionMissingError,
    SubscriptionSpec,
)

OURA_SUBSCRIPTION_DATA_TYPES = (
    "daily_sleep",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "sleep",
    "workout",
)
OURA_SUBSCRIPTION_EVENT_TYPES = ("create", "update")
OURA_RENEWAL_WINDOW = timedelta(days=30)
OURA_REMOTE_SUBSCRIPTION_MAX = 500


@dataclass(frozen=True)
class OuraCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OuraSubscriptionContext:
    credentials: OuraCredentials
    callback_url: str
    verification_token: str
    owned_callback_urls: frozenset[str]


def _stored_secret(raw: str) -> str:
    try:
        return decrypt(raw)
    except Exception:
        return raw


def _parse_expiration(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise RuntimeError("Oura subscription expiration_time is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RuntimeError("Oura subscription expiration_time is invalid") from None
    if parsed.tzinfo is None:
        raise RuntimeError("Oura subscription expiration_time has no timezone")
    return parsed.astimezone(UTC)


def parse_oura_subscription(payload: object) -> RemoteSubscription:
    if not isinstance(payload, dict):
        raise RuntimeError("Oura subscription response is not an object")
    remote_id = payload.get("id")
    callback_url = payload.get("callback_url")
    event_type = payload.get("event_type")
    data_type = payload.get("data_type")
    if not isinstance(remote_id, str) or not remote_id or len(remote_id) > 200:
        raise RuntimeError("Oura subscription id is missing or invalid")
    if not isinstance(callback_url, str) or not callback_url or len(callback_url) > 2048:
        raise RuntimeError("Oura subscription callback_url is missing or invalid")
    if event_type not in {"create", "update", "delete"}:
        raise RuntimeError("Oura subscription event_type is invalid")
    if not isinstance(data_type, str) or not data_type or len(data_type) > 100:
        raise RuntimeError("Oura subscription data_type is invalid")
    return RemoteSubscription(
        key=f"{event_type}:{data_type}",
        remote_id=remote_id,
        renew_at=_parse_expiration(payload.get("expiration_time")),
        provider_data=json.dumps({"callback_url": callback_url}, separators=(",", ":"), sort_keys=True),
    )


def _is_missing(error: httpx.HTTPStatusError) -> bool:
    # Oura documents 403, rather than 404, when a webhook ID does not exist.
    return error.response.status_code == 403


class OuraSubscriptionAdapter:
    provider = "oura"
    renewal_window = OURA_RENEWAL_WINDOW
    operation_timeout_seconds = 35.0

    def desired_specs(self) -> tuple[SubscriptionSpec, ...]:
        return tuple(
            SubscriptionSpec(f"{event_type}:{data_type}")
            for data_type in OURA_SUBSCRIPTION_DATA_TYPES
            for event_type in OURA_SUBSCRIPTION_EVENT_TYPES
        )

    async def prepare(self, db, user_id: str, registration: dict) -> OuraSubscriptionContext:
        rows = await db.execute_fetchall(
            """SELECT client_id, client_secret_enc, webhook_secret
               FROM integrations WHERE provider = 'oura'"""
        )
        if not rows or not rows[0]["client_id"] or not rows[0]["client_secret_enc"]:
            raise RuntimeError("Oura application credentials are unavailable")
        credentials = OuraCredentials(rows[0]["client_id"], decrypt(rows[0]["client_secret_enc"]))
        webhook_ids = await get_webhook_ids_for_user(user_id, self.provider)
        current_id = await get_webhook_id_for_user(user_id, self.provider)
        callback_url = registration["endpoint"]
        if not callback_url:
            if not current_id and registration["desired_state"] == "enabled":
                raise RuntimeError("Oura callback route is unavailable")
            callback_url = f"{BASE_URL.rstrip('/')}/api/oura/webhook/{current_id}" if current_id else ""
        verification_token = _stored_secret(rows[0]["webhook_secret"]) if rows[0]["webhook_secret"] else ""
        if registration["desired_state"] == "enabled" and not verification_token:
            raise RuntimeError("Oura webhook verification token is unavailable")
        base_callback = f"{BASE_URL.rstrip('/')}/api/oura/webhook"
        owned_callbacks = {base_callback, f"{base_callback}/"}
        if callback_url:
            owned_callbacks.add(callback_url)
        owned_callbacks.update(f"{base_callback}/{webhook_id}" for webhook_id in webhook_ids)
        return OuraSubscriptionContext(
            credentials=credentials,
            callback_url=callback_url,
            verification_token=verification_token,
            owned_callback_urls=frozenset(owned_callbacks),
        )

    def credential_key(self, context: OuraSubscriptionContext) -> str:
        return context.credentials.client_id

    def endpoint(self, context: OuraSubscriptionContext) -> str:
        return context.callback_url

    def owns_remote(
        self,
        context: OuraSubscriptionContext,
        item: RemoteSubscription,
        tracked_remote_ids: set[str],
    ) -> bool:
        if item.remote_id in tracked_remote_ids:
            return True
        try:
            callback_url = json.loads(item.provider_data)["callback_url"]
        except (KeyError, TypeError, json.JSONDecodeError):
            return False
        return callback_url in context.owned_callback_urls

    def matches_desired(
        self,
        context: OuraSubscriptionContext,
        item: RemoteSubscription,
        spec: SubscriptionSpec,
    ) -> bool:
        if item.key != spec.key:
            return False
        try:
            return json.loads(item.provider_data)["callback_url"] == context.callback_url
        except (KeyError, TypeError, json.JSONDecodeError):
            return False

    async def list_remote(self, context: OuraSubscriptionContext) -> list[RemoteSubscription]:
        credentials = context.credentials
        payload = await list_oura_webhook_subscriptions(credentials.client_id, credentials.client_secret)
        if len(payload) > OURA_REMOTE_SUBSCRIPTION_MAX:
            raise RuntimeError("Oura returned too many webhook subscriptions")
        return [parse_oura_subscription(item) for item in payload]

    async def create_remote(
        self,
        context: OuraSubscriptionContext,
        spec: SubscriptionSpec,
    ) -> RemoteSubscription:
        event_type, separator, data_type = spec.key.partition(":")
        if not separator or event_type not in OURA_SUBSCRIPTION_EVENT_TYPES:
            raise ValueError("Unsupported Oura subscription specification")
        if data_type not in OURA_SUBSCRIPTION_DATA_TYPES:
            raise ValueError("Unsupported Oura subscription data type")
        credentials = context.credentials
        payload = await create_oura_webhook_subscription(
            credentials.client_id,
            credentials.client_secret,
            context.callback_url,
            context.verification_token,
            event_type,
            data_type,
        )
        return parse_oura_subscription(payload)

    async def renew_remote(self, context: OuraSubscriptionContext, remote_id: str) -> RemoteSubscription:
        credentials = context.credentials
        try:
            payload = await renew_oura_webhook_subscription(credentials.client_id, credentials.client_secret, remote_id)
        except httpx.HTTPStatusError as error:
            if _is_missing(error):
                raise SubscriptionMissingError("Oura subscription no longer exists") from error
            raise
        return parse_oura_subscription(payload)

    async def delete_remote(self, context: OuraSubscriptionContext, remote_id: str) -> None:
        credentials = context.credentials
        try:
            await delete_oura_webhook_subscription(credentials.client_id, credentials.client_secret, remote_id)
        except httpx.HTTPStatusError as error:
            if _is_missing(error):
                raise SubscriptionMissingError("Oura subscription no longer exists") from error
            raise

    def renewal_due(self, _context: OuraSubscriptionContext, item: RemoteSubscription, now: datetime) -> bool:
        if item.renew_at is None:
            return True
        return item.renew_at <= now + self.renewal_window

    def next_reconcile_at(
        self,
        _context: OuraSubscriptionContext,
        items: list[RemoteSubscription],
        now: datetime,
    ) -> datetime:
        latest_allowed = now + timedelta(hours=24)
        renewals = [item.renew_at - self.renewal_window for item in items if item.renew_at is not None]
        if not renewals:
            return now + timedelta(hours=1)
        return max(now + timedelta(minutes=5), min(latest_allowed, min(renewals)))

    def renewal_due_at(self, _context: OuraSubscriptionContext, items: list[RemoteSubscription]) -> str:
        renewals = [item.renew_at - self.renewal_window for item in items if item.renew_at is not None]
        return min(renewals).astimezone(UTC).isoformat() if renewals else ""

    async def finalize_disable(self, db, user_id: str) -> None:
        await db.execute("UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'")
        await set_setting(db, "oura_webhook_id", "")
        await delete_webhook_routes(user_id, self.provider)


OURA_SUBSCRIPTION_ADAPTER = OuraSubscriptionAdapter()
