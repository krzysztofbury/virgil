"""Provider-neutral lifecycle for expiring remote subscriptions."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from app import central_db
from app.user_db import close_user_db, open_user_db

logger = logging.getLogger(__name__)

MAX_DESIRED_SUBSCRIPTIONS = 100
PROVIDER_OPERATION_TIMEOUT_MAX_SECONDS = 300.0
RECONCILE_RETRY_DELAY = timedelta(hours=1)


class SubscriptionMissingError(RuntimeError):
    """The provider confirms that a previously known remote item is absent."""


@dataclass(frozen=True)
class SubscriptionSpec:
    key: str


@dataclass(frozen=True)
class RemoteSubscription:
    key: str
    remote_id: str
    renew_at: datetime | None = None
    provider_data: str = "{}"

    def __post_init__(self) -> None:
        if not self.key or len(self.key) > 200:
            raise ValueError("Remote subscription key is required and bounded")
        if not self.remote_id or len(self.remote_id) > 200:
            raise ValueError("Remote subscription ID is required and bounded")
        if self.renew_at is not None and self.renew_at.tzinfo is None:
            raise ValueError("Remote subscription renewal time must include a timezone")
        if len(self.provider_data) > 4000:
            raise ValueError("Remote subscription provider data exceeds 4000 characters")


@dataclass(frozen=True)
class ReconcileResult:
    status: str
    item_count: int
    error: str = ""


class SubscriptionAdapter(Protocol):
    """Adapter contract for one provider's remote subscription resources.

    Every item returned by create_remote must be rediscoverable by list_remote
    and owns_remote without relying on a locally persisted remote ID.
    """

    provider: str
    operation_timeout_seconds: float

    def desired_specs(self) -> tuple[SubscriptionSpec, ...]: ...

    async def prepare(self, db: Any, user_id: str, registration: dict) -> Any: ...

    def credential_key(self, context: Any) -> str: ...

    def endpoint(self, context: Any) -> str: ...

    def owns_remote(self, context: Any, item: RemoteSubscription, tracked_remote_ids: set[str]) -> bool: ...

    def matches_desired(self, context: Any, item: RemoteSubscription, spec: SubscriptionSpec) -> bool: ...

    async def list_remote(self, context: Any) -> list[RemoteSubscription]: ...

    async def create_remote(self, context: Any, spec: SubscriptionSpec) -> RemoteSubscription: ...

    async def renew_remote(self, context: Any, remote_id: str) -> RemoteSubscription: ...

    async def delete_remote(self, context: Any, remote_id: str) -> None: ...

    def renewal_due(self, context: Any, item: RemoteSubscription, now: datetime) -> bool: ...

    def next_reconcile_at(self, context: Any, items: list[RemoteSubscription], now: datetime) -> datetime: ...

    def renewal_due_at(self, context: Any, items: list[RemoteSubscription]) -> str: ...

    async def finalize_disable(self, db: Any, user_id: str) -> None: ...


def _adapter_for(provider: str) -> SubscriptionAdapter:
    from app.services.oura_subscriptions import OURA_SUBSCRIPTION_ADAPTER

    adapters: dict[str, SubscriptionAdapter] = {OURA_SUBSCRIPTION_ADAPTER.provider: OURA_SUBSCRIPTION_ADAPTER}
    try:
        return adapters[provider]
    except KeyError:
        raise ValueError(f"Unsupported subscription provider: {provider}") from None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Subscription lifecycle timestamps must include a timezone")
    return value.astimezone(UTC)


def _bounded_error(error: object) -> str:
    text = str(error).strip() or error.__class__.__name__
    return text[:1000]


async def _provider_call[T](adapter: SubscriptionAdapter, operation: Callable[[], Awaitable[T]]) -> T:
    timeout_seconds = float(adapter.operation_timeout_seconds)
    if not 1.0 <= timeout_seconds <= PROVIDER_OPERATION_TIMEOUT_MAX_SECONDS:
        raise ValueError("Provider operation timeout must be between 1 and 300 seconds")
    return await asyncio.wait_for(operation(), timeout=timeout_seconds)


def _item_dict(item: RemoteSubscription) -> dict:
    return {
        "subscription_key": item.key,
        "remote_id": item.remote_id,
        "renew_at": item.renew_at.astimezone(UTC).isoformat() if item.renew_at else "",
        "provider_data": item.provider_data,
    }


async def enable_subscription(
    user_id: str,
    provider: str,
    endpoint: str = "",
    credential_key: str = "",
    *,
    lifecycle_claim: str = "",
) -> dict:
    if len(endpoint) > 2048:
        raise ValueError("Subscription endpoint exceeds 2048 characters")
    if len(credential_key) > 500:
        raise ValueError("Subscription credential key exceeds 500 characters")
    _adapter_for(provider)
    return await central_db.configure_subscription_registration(
        user_id,
        provider,
        endpoint,
        credential_key,
        lifecycle_claim,
    )


async def disable_subscription(user_id: str, provider: str) -> dict | None:
    _adapter_for(provider)
    return await central_db.request_subscription_disable(user_id, provider)


async def _heartbeat(registration: dict) -> None:
    current = await central_db.heartbeat_subscription_claim(
        registration["user_id"],
        registration["provider"],
        registration["claim_token"],
        registration["desired_revision"],
        registration["desired_state"],
    )
    if not current:
        raise RuntimeError("Subscription reconcile was superseded by newer desired state")


async def _reconcile_enabled(
    adapter: SubscriptionAdapter,
    registration: dict,
    context: Any,
    remote: list[RemoteSubscription],
    tracked_remote_ids: set[str],
    now: datetime,
) -> ReconcileResult:
    desired = adapter.desired_specs()
    if not desired or len(desired) > MAX_DESIRED_SUBSCRIPTIONS:
        raise RuntimeError("Provider desired subscription count is outside supported bounds")
    desired_by_key = {spec.key: spec for spec in desired}
    if len(desired_by_key) != len(desired):
        raise RuntimeError("Provider desired subscription keys are not unique")

    owned = [item for item in remote if adapter.owns_remote(context, item, tracked_remote_ids)]
    errors: list[str] = []
    confirmed: list[RemoteSubscription] = []
    by_key: dict[str, list[RemoteSubscription]] = {}
    for item in owned:
        spec = desired_by_key.get(item.key)
        if spec is None or adapter.matches_desired(context, item, spec):
            by_key.setdefault(item.key, []).append(item)
            continue
        try:
            await _heartbeat(registration)
            await _provider_call(adapter, lambda item=item: adapter.delete_remote(context, item.remote_id))
        except SubscriptionMissingError:
            pass
        except Exception as error:
            errors.append(f"delete stale {item.key}: {_bounded_error(error)}")

    for key, items in sorted(by_key.items()):
        if key in desired_by_key:
            continue
        for item in items:
            try:
                await _heartbeat(registration)
                await _provider_call(adapter, lambda item=item: adapter.delete_remote(context, item.remote_id))
            except SubscriptionMissingError:
                pass
            except Exception as error:
                errors.append(f"delete obsolete {key}: {_bounded_error(error)}")

    for key, spec in sorted(desired_by_key.items()):
        candidates = sorted(
            by_key.get(key, []),
            key=lambda item: item.renew_at or datetime.max.replace(tzinfo=UTC),
            reverse=True,
        )
        current = candidates[0] if candidates else None
        created_now = False
        for duplicate in candidates[1:]:
            try:
                await _heartbeat(registration)
                await _provider_call(
                    adapter, lambda duplicate=duplicate: adapter.delete_remote(context, duplicate.remote_id)
                )
            except SubscriptionMissingError:
                pass
            except Exception as error:
                errors.append(f"delete duplicate {key}: {_bounded_error(error)}")
        try:
            if current is None:
                await _heartbeat(registration)
                current = await _provider_call(adapter, lambda spec=spec: adapter.create_remote(context, spec))
                created_now = True
            elif adapter.renewal_due(context, current, now):
                try:
                    await _heartbeat(registration)
                    current = await _provider_call(
                        adapter, lambda current=current: adapter.renew_remote(context, current.remote_id)
                    )
                except SubscriptionMissingError:
                    await _heartbeat(registration)
                    current = await _provider_call(adapter, lambda spec=spec: adapter.create_remote(context, spec))
                    created_now = True
            if (
                current.key != key
                or not adapter.matches_desired(context, current, spec)
                or not adapter.owns_remote(context, current, set())
            ):
                if created_now:
                    await _heartbeat(registration)
                    await _provider_call(
                        adapter, lambda current=current: adapter.delete_remote(context, current.remote_id)
                    )
                raise RuntimeError("Provider returned a subscription that does not match the request")
            confirmed.append(current)
        except Exception as error:
            errors.append(f"reconcile {key}: {_bounded_error(error)}")

    status = "active" if len(confirmed) == len(desired) and not errors else "degraded"
    error_text = "; ".join(errors)[:1000]
    next_at = (
        now + RECONCILE_RETRY_DELAY if status == "degraded" else adapter.next_reconcile_at(context, confirmed, now)
    )
    await central_db.publish_subscription_reconcile(
        registration["user_id"],
        registration["provider"],
        registration["claim_token"],
        registration["desired_revision"],
        endpoint=adapter.endpoint(context),
        credential_key=adapter.credential_key(context),
        status=status,
        renewal_due_at=adapter.renewal_due_at(context, confirmed),
        next_reconcile_at=next_at.isoformat(),
        items=[_item_dict(item) for item in confirmed],
        last_error=error_text,
        now=now,
    )
    return ReconcileResult(status=status, item_count=len(confirmed), error=error_text)


async def _reconcile_disabled(
    adapter: SubscriptionAdapter,
    registration: dict,
    db: Any,
    context: Any,
    remote: list[RemoteSubscription],
    tracked_remote_ids: set[str],
) -> ReconcileResult:
    owned_ids = {item.remote_id for item in remote if adapter.owns_remote(context, item, tracked_remote_ids)}
    errors: list[str] = []
    for remote_id in sorted(owned_ids):
        try:
            await _heartbeat(registration)
            await _provider_call(adapter, lambda remote_id=remote_id: adapter.delete_remote(context, remote_id))
        except SubscriptionMissingError:
            pass
        except Exception as error:
            errors.append(_bounded_error(error))
    if errors:
        raise RuntimeError("; ".join(errors))

    await _heartbeat(registration)
    await adapter.finalize_disable(db, registration["user_id"])
    await central_db.delete_subscription_registration(
        registration["user_id"],
        registration["provider"],
        registration["claim_token"],
        registration["desired_revision"],
    )
    return ReconcileResult(status="disabled", item_count=0)


async def reconcile_subscription(
    user_id: str,
    provider: str,
    db: Any,
    *,
    worker_id: str,
    now: datetime | None = None,
) -> ReconcileResult | None:
    current_time = _utc(now or datetime.now(UTC))
    registration = await central_db.claim_subscription_registration(
        user_id,
        provider,
        worker_id,
        now=current_time,
    )
    if registration is None:
        return None

    adapter = _adapter_for(provider)
    try:
        context = await adapter.prepare(db, user_id, registration)
        credential_key = adapter.credential_key(context)
        if registration["credential_key"] and credential_key != registration["credential_key"]:
            raise RuntimeError("Provider credentials changed before existing subscriptions were removed")
        await _heartbeat(registration)
        remote = await _provider_call(adapter, lambda: adapter.list_remote(context))
        if len(remote) > MAX_DESIRED_SUBSCRIPTIONS * 10:
            raise RuntimeError("Provider returned too many remote subscriptions")
        tracked = await central_db.get_subscription_items(user_id, provider)
        tracked_remote_ids = {item["remote_id"] for item in tracked}
        if registration["desired_state"] == "disabled":
            return await _reconcile_disabled(adapter, registration, db, context, remote, tracked_remote_ids)
        return await _reconcile_enabled(
            adapter,
            registration,
            context,
            remote,
            tracked_remote_ids,
            current_time,
        )
    except Exception as error:
        failure_recorded = await central_db.fail_subscription_reconcile(
            user_id,
            provider,
            registration["claim_token"],
            registration["desired_revision"],
            registration["desired_state"],
            _bounded_error(error),
            (current_time + RECONCILE_RETRY_DELAY).isoformat(),
            now=current_time,
        )
        if not failure_recorded:
            await central_db.release_superseded_subscription_claim(user_id, provider, registration["claim_token"])
        raise


async def reconcile_due_subscriptions(
    *,
    worker_id: str,
    limit: int = 10,
    concurrency: int = 2,
) -> list[ReconcileResult]:
    if not 1 <= concurrency <= 10:
        raise ValueError("Subscription reconcile concurrency must be between 1 and 10")
    due = await central_db.list_due_subscription_registrations(limit)
    semaphore = asyncio.Semaphore(concurrency)
    results: list[ReconcileResult] = []

    async def reconcile_one(registration: dict) -> None:
        async with semaphore:
            db = None
            try:
                db = await open_user_db(registration["db_filename"])
                result = await reconcile_subscription(
                    registration["user_id"],
                    registration["provider"],
                    db,
                    worker_id=worker_id,
                )
                if result is not None:
                    results.append(result)
            except Exception:
                logger.exception(
                    "Subscription reconcile failed for %s/%s",
                    registration["user_id"],
                    registration["provider"],
                )
            finally:
                if db is not None:
                    try:
                        await close_user_db(db)
                    except Exception:
                        logger.exception("Failed to close subscription reconcile database")

    await asyncio.gather(*(reconcile_one(registration) for registration in due))
    return results
