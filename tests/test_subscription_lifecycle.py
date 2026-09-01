"""Generic subscription lifecycle and Oura adapter behavior."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.services.subscriptions import RemoteSubscription, SubscriptionSpec


async def _central_database(path):
    from app.central_migrations.runner import run_migrations

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await run_migrations(db)
    await db.execute(
        """INSERT INTO users (id, email, password_hash, db_filename)
           VALUES ('user-1', 'owner@example.com', 'hash', 'owner.db')"""
    )
    await db.commit()
    return db


class FakeSubscriptionAdapter:
    provider = "fake"
    renewal_window = timedelta(days=30)
    operation_timeout_seconds = 5.0

    def __init__(self, remote):
        self.remote = remote
        self.created = []
        self.renewed = []
        self.deleted = []
        self.finalized = False

    def desired_specs(self):
        return (SubscriptionSpec("create:sleep"), SubscriptionSpec("update:sleep"))

    async def prepare(self, _db, _user_id, registration):
        return {"endpoint": registration["endpoint"]}

    def credential_key(self, _context):
        return "application-1"

    def endpoint(self, context):
        return context["endpoint"]

    def owns_remote(self, context, item, tracked_remote_ids):
        return item.remote_id in tracked_remote_ids or item.provider_data == context["endpoint"]

    def matches_desired(self, context, item, spec):
        return item.key == spec.key and item.provider_data == context["endpoint"]

    async def list_remote(self, _context):
        return list(self.remote)

    async def create_remote(self, context, spec):
        self.created.append(spec.key)
        item = RemoteSubscription(
            spec.key,
            f"created-{spec.key}",
            datetime(2027, 9, 1, tzinfo=UTC),
            context["endpoint"],
        )
        self.remote.append(item)
        return item

    async def renew_remote(self, context, remote_id):
        self.renewed.append(remote_id)
        return RemoteSubscription(
            "create:sleep",
            remote_id,
            datetime(2027, 9, 1, tzinfo=UTC),
            context["endpoint"],
        )

    async def delete_remote(self, _context, remote_id):
        self.deleted.append(remote_id)
        self.remote = [item for item in self.remote if item.remote_id != remote_id]

    def renewal_due(self, _context, item, now):
        return item.renew_at is None or item.renew_at <= now + self.renewal_window

    def next_reconcile_at(self, _context, _items, now):
        return now + timedelta(hours=24)

    def renewal_due_at(self, _context, items):
        renewals = [item.renew_at for item in items if item.renew_at]
        return min(renewals).isoformat() if renewals else ""

    async def finalize_disable(self, _db, _user_id):
        self.finalized = True


def test_reconcile_renews_creates_preserves_foreign_and_disables(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        now = datetime(2026, 9, 1, tzinfo=UTC)
        callback = "https://virgil.example/api/fake/user-1"
        own_due = RemoteSubscription("create:sleep", "own-due", now + timedelta(days=1), callback)
        foreign = RemoteSubscription(
            "update:sleep",
            "foreign",
            now + timedelta(days=200),
            "https://other.example/api/fake/user-2",
        )
        adapter = FakeSubscriptionAdapter([own_due, foreign])
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)

        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            await subscriptions.enable_subscription("user-1", "fake", callback, "application-1")
            result = await subscriptions.reconcile_subscription(
                "user-1", "fake", object(), worker_id="test-worker", now=now
            )
            assert result is not None
            assert result.status == "active"
            assert result.item_count == 2
            assert adapter.renewed == ["own-due"]
            assert adapter.created == ["update:sleep"]
            assert adapter.deleted == []

            registration = await central_db.get_subscription_registration("user-1", "fake")
            assert registration["status"] == "active"
            assert registration["claim_token"] == ""
            items = await central_db.get_subscription_items("user-1", "fake")
            assert [item["subscription_key"] for item in items] == ["create:sleep", "update:sleep"]

            adapter.remote = [
                RemoteSubscription(
                    item["subscription_key"],
                    item["remote_id"],
                    datetime.fromisoformat(item["renew_at"]),
                    item["provider_data"],
                )
                for item in items
            ] + [foreign]
            await subscriptions.disable_subscription("user-1", "fake")
            disabled = await subscriptions.reconcile_subscription(
                "user-1", "fake", object(), worker_id="test-worker", now=now
            )
            assert disabled is not None
            assert disabled.status == "disabled"
            assert set(adapter.deleted) == {"own-due", "created-update:sleep"}
            assert "foreign" not in adapter.deleted
            assert adapter.finalized is True
            assert await central_db.get_subscription_registration("user-1", "fake") is None
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_reconcile_failure_is_persisted_and_releases_claim(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        adapter = FakeSubscriptionAdapter([])

        async def fail_list(_credentials):
            raise RuntimeError("provider unavailable")

        adapter.list_remote = fail_list
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)
        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            await subscriptions.enable_subscription(
                "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
            )
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await subscriptions.reconcile_subscription(
                    "user-1",
                    "fake",
                    object(),
                    worker_id="test-worker",
                    now=datetime(2026, 9, 1, tzinfo=UTC),
                )
            registration = await central_db.get_subscription_registration("user-1", "fake")
            assert registration["status"] == "error"
            assert registration["last_error"] == "provider unavailable"
            assert registration["claim_token"] == ""
            assert registration["claim_owner"] == ""
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_new_desired_state_fences_stale_reconcile_publication(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        adapter = FakeSubscriptionAdapter([])
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)
        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            await subscriptions.enable_subscription(
                "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
            )
            claimed = await central_db.claim_subscription_registration("user-1", "fake", "worker-1")
            await subscriptions.disable_subscription("user-1", "fake")

            with pytest.raises(RuntimeError, match="superseded"):
                await central_db.publish_subscription_reconcile(
                    "user-1",
                    "fake",
                    claimed["claim_token"],
                    claimed["desired_revision"],
                    endpoint="https://virgil.example/api/fake/user-1",
                    credential_key="application-1",
                    status="active",
                    renewal_due_at="",
                    next_reconcile_at=datetime(2026, 9, 2, tzinfo=UTC).isoformat(),
                    items=[],
                )
            registration = await central_db.get_subscription_registration("user-1", "fake")
            assert registration["desired_state"] == "disabled"
            assert registration["status"] == "disabling"
            assert registration["claim_token"] == claimed["claim_token"]
            assert await central_db.release_superseded_subscription_claim("user-1", "fake", claimed["claim_token"])
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_disable_waits_for_inflight_create_then_removes_created_resource(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        adapter = FakeSubscriptionAdapter([])
        create_started = asyncio.Event()
        release_create = asyncio.Event()
        original_create = adapter.create_remote

        async def blocked_create(context, spec):
            create_started.set()
            await release_create.wait()
            return await original_create(context, spec)

        adapter.create_remote = blocked_create
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)
        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            now = datetime(2026, 9, 1, tzinfo=UTC)
            await subscriptions.enable_subscription(
                "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
            )
            enabling = asyncio.create_task(
                subscriptions.reconcile_subscription("user-1", "fake", object(), worker_id="enable-worker", now=now)
            )
            await asyncio.wait_for(create_started.wait(), timeout=1)
            await subscriptions.disable_subscription("user-1", "fake")
            assert (
                await subscriptions.reconcile_subscription(
                    "user-1", "fake", object(), worker_id="disable-worker", now=now
                )
                is None
            )

            release_create.set()
            with pytest.raises(RuntimeError, match="superseded"):
                await enabling
            registration = await central_db.get_subscription_registration("user-1", "fake")
            assert registration["claim_token"] == ""
            assert registration["desired_state"] == "disabled"

            disabled = await subscriptions.reconcile_subscription(
                "user-1", "fake", object(), worker_id="disable-worker", now=now
            )
            assert disabled is not None
            assert disabled.status == "disabled"
            assert set(adapter.deleted) == {"created-create:sleep"}
            assert adapter.remote == []
            assert await central_db.get_subscription_registration("user-1", "fake") is None
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_factory_reset_lease_blocks_new_subscription_registration(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        adapter = FakeSubscriptionAdapter([])
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)
        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            claim = await central_db.claim_user_lifecycle("user-1", "factory-reset")
            assert claim is not None
            with pytest.raises(RuntimeError, match="factory-reset"):
                await subscriptions.enable_subscription(
                    "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
                )
            assert await central_db.get_subscription_registration("user-1", "fake") is None

            await central_db.release_user_lifecycle("user-1", claim)
            registration = await subscriptions.enable_subscription(
                "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
            )
            assert registration["desired_state"] == "enabled"
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_item_publication_rolls_back_as_one_fenced_transaction(tmp_path, monkeypatch):
    async def scenario():
        import app.central_db as central_db
        import app.services.subscriptions as subscriptions

        adapter = FakeSubscriptionAdapter([])
        monkeypatch.setattr(subscriptions, "_adapter_for", lambda provider: adapter)
        db = await _central_database(tmp_path / "central.db")
        previous = central_db._central_db
        central_db._central_db = db
        try:
            await subscriptions.enable_subscription(
                "user-1", "fake", "https://virgil.example/api/fake/user-1", "application-1"
            )
            first = await central_db.claim_subscription_registration("user-1", "fake", "worker-1")
            await central_db.publish_subscription_reconcile(
                "user-1",
                "fake",
                first["claim_token"],
                first["desired_revision"],
                endpoint="https://virgil.example/api/fake/user-1",
                credential_key="application-1",
                status="active",
                renewal_due_at="",
                next_reconcile_at=datetime(2026, 9, 2, tzinfo=UTC).isoformat(),
                items=[
                    {
                        "subscription_key": "existing",
                        "remote_id": "remote-existing",
                        "renew_at": "",
                        "provider_data": "{}",
                    }
                ],
            )
            second = await central_db.claim_subscription_registration("user-1", "fake", "worker-2")
            with pytest.raises(sqlite3.IntegrityError):
                await central_db.publish_subscription_reconcile(
                    "user-1",
                    "fake",
                    second["claim_token"],
                    second["desired_revision"],
                    endpoint="https://virgil.example/api/fake/user-1",
                    credential_key="application-1",
                    status="active",
                    renewal_due_at="",
                    next_reconcile_at=datetime(2026, 9, 2, tzinfo=UTC).isoformat(),
                    items=[
                        {"subscription_key": "one", "remote_id": "duplicate", "provider_data": "{}"},
                        {"subscription_key": "two", "remote_id": "duplicate", "provider_data": "{}"},
                    ],
                )
            items = await central_db.get_subscription_items("user-1", "fake")
            assert [(item["subscription_key"], item["remote_id"]) for item in items] == [
                ("existing", "remote-existing")
            ]
        finally:
            central_db._central_db = previous
            await db.close()

    asyncio.run(scenario())


def test_oura_adapter_has_canonical_specs_and_requires_aware_expiry():
    from app.services.oura_subscriptions import (
        OURA_SUBSCRIPTION_ADAPTER,
        OURA_SUBSCRIPTION_DATA_TYPES,
        OURA_SUBSCRIPTION_EVENT_TYPES,
        OuraCredentials,
        OuraSubscriptionContext,
        parse_oura_subscription,
    )

    assert {spec.key for spec in OURA_SUBSCRIPTION_ADAPTER.desired_specs()} == {
        f"{event_type}:{data_type}"
        for data_type in OURA_SUBSCRIPTION_DATA_TYPES
        for event_type in OURA_SUBSCRIPTION_EVENT_TYPES
    }
    parsed = parse_oura_subscription(
        {
            "id": "subscription-1",
            "callback_url": "https://virgil.example/api/oura/webhook/abc",
            "event_type": "create",
            "data_type": "sleep",
            "expiration_time": "2027-09-01T12:00:00Z",
        }
    )
    assert parsed.renew_at == datetime(2027, 9, 1, 12, tzinfo=UTC)
    context = OuraSubscriptionContext(
        credentials=OuraCredentials("client", "secret"),
        callback_url="https://virgil.example/api/oura/webhook/current",
        verification_token="token",
        owned_callback_urls=frozenset(
            {
                "https://virgil.example/api/oura/webhook/current",
                "https://virgil.example/api/oura/webhook/old-route",
                "https://virgil.example/api/oura/webhook",
            }
        ),
    )
    legacy = RemoteSubscription(
        "create:sleep", "legacy", provider_data='{"callback_url":"https://virgil.example/api/oura/webhook"}'
    )
    alias = RemoteSubscription(
        "create:sleep", "alias", provider_data='{"callback_url":"https://virgil.example/api/oura/webhook/old-route"}'
    )
    foreign = RemoteSubscription(
        "create:sleep", "foreign", provider_data='{"callback_url":"https://other.example/webhook"}'
    )
    assert OURA_SUBSCRIPTION_ADAPTER.owns_remote(context, legacy, set()) is True
    assert OURA_SUBSCRIPTION_ADAPTER.owns_remote(context, alias, set()) is True
    assert OURA_SUBSCRIPTION_ADAPTER.owns_remote(context, foreign, set()) is False

    with pytest.raises(RuntimeError, match="no timezone"):
        parse_oura_subscription(
            {
                "id": "subscription-1",
                "callback_url": "https://virgil.example/api/oura/webhook/abc",
                "event_type": "create",
                "data_type": "sleep",
                "expiration_time": "2027-09-01T12:00:00",
            }
        )


def test_oura_renewal_uses_documented_endpoint(monkeypatch):
    async def scenario():
        import app.services.oura_api as oura_api

        calls = []

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"id": "subscription-1"}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def put(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()

        monkeypatch.setattr(oura_api.httpx, "AsyncClient", lambda **_kwargs: Client())
        await oura_api.renew_oura_webhook_subscription("client", "secret", "subscription-1")
        assert calls == [
            (
                f"{oura_api.OURA_WEBHOOK_URL}/renew/subscription-1",
                {"headers": {"x-client-id": "client", "x-client-secret": "secret"}},
            )
        ]

    asyncio.run(scenario())
