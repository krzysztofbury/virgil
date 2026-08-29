"""Bounded durable-job execution and scheduler integration."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.services.job_worker import AmbiguousJobError, run_jobs_for_user
from app.services.jobs import claim_next_job, enqueue_job, get_job, recover_stale_jobs


async def _open(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def _setup(path):
    db = await _open(path)
    migration = importlib.import_module("app.migrations.027_jobs")
    await migration.up(db)
    await db.execute("CREATE TABLE domain_writes (value TEXT NOT NULL)")
    await db.commit()
    return db


def _connections(path):
    opened = []

    async def opener(_filename):
        db = await _open(path)
        opened.append(db)
        return db

    async def closer(db):
        await db.close()

    return opened, opener, closer


def test_handler_runs_after_claim_commit_on_a_separate_connection(tmp_path):
    async def scenario():
        path = tmp_path / "success.db"
        control = await _setup(path)
        opened, opener, closer = _connections(path)
        try:
            queued = await enqueue_job(control, "test_write", {"value": "saved"})

            async def handler(context, payload):
                assert context.db is not control
                assert control.in_transaction is False
                await context.db.execute("INSERT INTO domain_writes VALUES (?)", (payload["value"],))
                await context.db.commit()
                return {"stored": True}

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={"test_write": handler},
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            job = await get_job(control, queued.job_id)
            writes = await control.execute_fetchall("SELECT value FROM domain_writes")
            return batch, job, [row["value"] for row in writes], opened
        finally:
            await control.close()

    batch, job, writes, opened = asyncio.run(scenario())
    assert [(item.job_id, item.status) for item in batch.jobs] == [(job["id"], "succeeded")]
    assert batch.recovered == ()
    assert job["result_json"] == '{"stored":true}'
    assert writes == ["saved"]
    assert len(opened) == 1


def test_unknown_kind_needs_attention_without_opening_handler_db(tmp_path):
    async def scenario():
        path = tmp_path / "unknown.db"
        control = await _setup(path)
        try:
            queued = await enqueue_job(control, "unknown_work", retry_policy="automatic")

            async def unexpected_open(_filename):
                raise AssertionError("unknown work must not open a handler database")

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={},
                handler_db_opener=unexpected_open,
            )
            return batch, await get_job(control, queued.job_id)
        finally:
            await control.close()

    batch, job = asyncio.run(scenario())
    assert [item.status for item in batch.jobs] == ["needs_attention"]
    assert job["status"] == "needs_attention"
    assert job["last_error"] == "Unsupported job kind."


def test_automatic_failure_requeues_with_bounded_backoff_and_public_error(tmp_path, monkeypatch):
    async def scenario():
        path = tmp_path / "failure.db"
        control = await _setup(path)
        _, opener, closer = _connections(path)
        try:
            queued = await enqueue_job(control, "failing_work", max_attempts=2, retry_policy="automatic")

            async def handler(_context, _payload):
                raise RuntimeError("private provider detail")

            before = datetime.now(UTC)
            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={"failing_work": handler},
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            return before, batch, await get_job(control, queued.job_id)
        finally:
            await control.close()

    monkeypatch.setattr("app.services.job_worker.secrets.randbelow", lambda _limit: 0)
    before, batch, job = asyncio.run(scenario())
    retry_at = datetime.strptime(job["run_after"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    assert [item.status for item in batch.jobs] == ["queued"]
    assert 59 <= (retry_at - before).total_seconds() <= 61
    assert job["last_error"] == "Job execution failed."


@pytest.mark.parametrize("mode", ["ambiguous", "self_cancel", "timeout", "oversized_result"])
def test_uncertain_handler_outcomes_need_attention(tmp_path, mode):
    async def scenario():
        path = tmp_path / f"{mode}.db"
        control = await _setup(path)
        _, opener, closer = _connections(path)
        cancelled = asyncio.Event()
        try:
            queued = await enqueue_job(control, "uncertain_work")

            async def handler(_context, _payload):
                if mode == "ambiguous":
                    raise AmbiguousJobError("Provider result is unknown.")
                if mode == "self_cancel":
                    raise asyncio.CancelledError
                if mode == "oversized_result":
                    return {"value": "x" * 17000}
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={"uncertain_work": handler},
                heartbeat_seconds=0.01,
                stale_seconds=6,
                execution_timeout_seconds=0.02,
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            return batch, await get_job(control, queued.job_id), cancelled.is_set()
        finally:
            await control.close()

    batch, job, was_cancelled = asyncio.run(scenario())
    assert [item.status for item in batch.jobs] == ["needs_attention"]
    assert job["status"] == "needs_attention"
    if mode == "timeout":
        assert was_cancelled is True


def test_worker_cancellation_leaves_running_job_for_stale_recovery(tmp_path):
    async def scenario():
        path = tmp_path / "cancel.db"
        control = await _setup(path)
        opened, opener, closer = _connections(path)
        started = asyncio.Event()
        handler_cancelled = asyncio.Event()
        try:
            queued = await enqueue_job(control, "slow_work")

            async def handler(_context, _payload):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    handler_cancelled.set()

            worker = asyncio.create_task(
                run_jobs_for_user(
                    control,
                    "user-1",
                    path.name,
                    worker_id="worker-a",
                    handlers={"slow_work": handler},
                    heartbeat_seconds=0.05,
                    stale_seconds=6,
                    execution_timeout_seconds=10,
                    handler_db_opener=opener,
                    handler_db_closer=closer,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            return await get_job(control, queued.job_id), handler_cancelled.is_set(), opened
        finally:
            await control.close()

    job, handler_cancelled, opened = asyncio.run(scenario())
    assert job["status"] == "running"
    assert handler_cancelled is True
    assert len(opened) == 1


def test_stale_job_is_recovered_before_next_job_is_claimed(tmp_path):
    async def scenario():
        path = tmp_path / "restart.db"
        control = await _setup(path)
        _, opener, closer = _connections(path)
        old = datetime.now(UTC) - timedelta(minutes=5)
        try:
            first = await enqueue_job(control, "test_work", {"order": 1}, run_after=old)
            second = await enqueue_job(control, "test_work", {"order": 2}, run_after=old)
            await claim_next_job(control, "dead-worker", now=old)

            async def handler(_context, payload):
                return {"order": payload["order"]}

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-b",
                handlers={"test_work": handler},
                stale_seconds=90,
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            return batch, await get_job(control, first.job_id), await get_job(control, second.job_id)
        finally:
            await control.close()

    batch, first, second = asyncio.run(scenario())
    assert [(item.job_id, item.status) for item in batch.recovered] == [(first["id"], "needs_attention")]
    assert [(item.job_id, item.status) for item in batch.jobs] == [(second["id"], "succeeded")]
    assert first["status"] == "needs_attention"
    assert second["status"] == "succeeded"


def test_lease_loss_cancels_old_handler_without_overwriting_recovery(tmp_path):
    async def scenario():
        path = tmp_path / "lease-loss.db"
        control = await _setup(path)
        recovery = await _open(path)
        _, opener, closer = _connections(path)
        started = asyncio.Event()
        cancelled = asyncio.Event()
        try:
            queued = await enqueue_job(control, "slow_work")

            async def handler(_context, _payload):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            worker = asyncio.create_task(
                run_jobs_for_user(
                    control,
                    "user-1",
                    path.name,
                    worker_id="worker-a",
                    handlers={"slow_work": handler},
                    heartbeat_seconds=0.02,
                    stale_seconds=6,
                    execution_timeout_seconds=10,
                    handler_db_opener=opener,
                    handler_db_closer=closer,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            forced_now = datetime.now(UTC) + timedelta(seconds=1)
            recovered = await recover_stale_jobs(recovery, forced_now, now=forced_now)
            batch = await asyncio.wait_for(worker, timeout=1)
            return recovered, batch, await get_job(control, queued.job_id), cancelled.is_set()
        finally:
            await control.close()
            await recovery.close()

    recovered, batch, job, cancelled = asyncio.run(scenario())
    assert [(item.job_id, item.status) for item in recovered] == [(job["id"], "needs_attention")]
    assert [item.status for item in batch.jobs] == ["lease_lost"]
    assert job["status"] == "needs_attention"
    assert cancelled is True


def test_worker_stops_at_explicit_batch_limit(tmp_path):
    async def scenario():
        path = tmp_path / "bounded.db"
        control = await _setup(path)
        _, opener, closer = _connections(path)
        try:
            for index in range(3):
                await enqueue_job(control, "test_work", {"index": index})

            async def handler(_context, payload):
                return {"index": payload["index"]}

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={"test_work": handler},
                max_jobs=2,
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            rows = await control.execute_fetchall("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            return batch, {row["status"]: row["n"] for row in rows}
        finally:
            await control.close()

    batch, statuses = asyncio.run(scenario())
    assert len(batch.jobs) == 2
    assert statuses == {"queued": 1, "succeeded": 2}


@pytest.mark.parametrize("hold_across_await", [False, True])
def test_handler_open_transaction_is_rolled_back_and_needs_attention(tmp_path, hold_across_await):
    async def scenario():
        path = tmp_path / f"transaction-{hold_across_await}.db"
        control = await _setup(path)
        _, opener, closer = _connections(path)
        try:
            queued = await enqueue_job(control, "bad_transaction")

            async def handler(context, _payload):
                await context.db.execute("INSERT INTO domain_writes VALUES ('uncommitted')")
                if hold_across_await:
                    await asyncio.sleep(0.1)
                return {"stored": True}

            batch = await run_jobs_for_user(
                control,
                "user-1",
                path.name,
                worker_id="worker-a",
                handlers={"bad_transaction": handler},
                heartbeat_seconds=0.01,
                stale_seconds=6,
                execution_timeout_seconds=0.2,
                handler_db_opener=opener,
                handler_db_closer=closer,
            )
            writes = await control.execute_fetchall("SELECT value FROM domain_writes")
            return batch, await get_job(control, queued.job_id), writes
        finally:
            await control.close()

    batch, job, writes = asyncio.run(scenario())
    assert [item.status for item in batch.jobs] == ["needs_attention"]
    assert job["status"] == "needs_attention"
    expected_error = (
        "Job handler held an open database transaction across an await."
        if hold_across_await
        else "Job handler returned with an uncommitted database transaction."
    )
    assert job["last_error"] == expected_error
    assert writes == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_jobs": 0},
        {"heartbeat_seconds": 2, "stale_seconds": 2},
        {"heartbeat_seconds": 2, "execution_timeout_seconds": 1},
    ],
)
def test_worker_rejects_unbounded_or_inconsistent_limits(tmp_path, overrides):
    async def scenario():
        control = await _setup(tmp_path / "limits.db")
        try:
            with pytest.raises(ValueError):
                await run_jobs_for_user(
                    control,
                    "user-1",
                    "limits.db",
                    worker_id="worker-a",
                    handlers={},
                    **overrides,
                )
        finally:
            await control.close()

    asyncio.run(scenario())


def test_scheduler_tick_bounds_users_isolates_failures_and_closes_connections(monkeypatch):
    async def scenario():
        import app.services.backup as backup_module
        import app.services.job_worker as worker_module
        import app.services.scheduler as scheduler

        users = [
            {"id": "1", "email": "one@example.com", "db_filename": "one.db"},
            {"id": "2", "email": "two@example.com", "db_filename": "two.db"},
            {"id": "3", "email": "three@example.com", "db_filename": "three.db"},
        ]
        calls = []

        async def active_users():
            return users

        async def open_db(filename):
            calls.append(("open", filename))
            return filename

        async def close_db(db):
            calls.append(("close", db))
            if db == "one.db":
                raise RuntimeError("isolated close failure")

        async def run_jobs(db, user_id, db_filename, *, worker_id):
            calls.append(("jobs", db, user_id, db_filename, worker_id))
            if user_id == "1":
                raise RuntimeError("isolated failure")

        async def legacy_tasks(db, user_id):
            calls.append(("legacy", db, user_id))

        async def central_backup():
            calls.append(("central",))

        monkeypatch.setattr(scheduler, "USERS_PER_TICK_MAX", 2)
        monkeypatch.setattr(scheduler, "USERS_CONCURRENT_MAX", 1)
        monkeypatch.setattr(scheduler, "_user_offset", 0)
        monkeypatch.setattr(scheduler, "get_active_users", active_users)
        monkeypatch.setattr(scheduler, "open_user_db", open_db)
        monkeypatch.setattr(scheduler, "close_user_db", close_db)
        monkeypatch.setattr(scheduler, "_check_and_run", legacy_tasks)
        monkeypatch.setattr(worker_module, "run_jobs_for_user", run_jobs)
        monkeypatch.setattr(backup_module, "maybe_backup_central", central_backup)

        await scheduler.scheduler_tick()
        return calls, scheduler.WORKER_ID

    calls, worker_id = asyncio.run(scenario())
    assert calls == [
        ("open", "one.db"),
        ("jobs", "one.db", "1", "one.db", worker_id),
        ("close", "one.db"),
        ("open", "two.db"),
        ("jobs", "two.db", "2", "two.db", worker_id),
        ("legacy", "two.db", "2"),
        ("close", "two.db"),
        ("central",),
    ]


def test_scheduler_user_batches_rotate_without_starvation(monkeypatch):
    import app.services.scheduler as scheduler

    users = [{"id": str(index)} for index in range(1, 5)]
    monkeypatch.setattr(scheduler, "USERS_PER_TICK_MAX", 2)
    monkeypatch.setattr(scheduler, "_user_offset", 0)

    assert [user["id"] for user in scheduler._select_users_for_tick(users)] == ["1", "2"]
    assert [user["id"] for user in scheduler._select_users_for_tick(users)] == ["3", "4"]
    assert [user["id"] for user in scheduler._select_users_for_tick(users)] == ["1", "2"]


def test_scheduler_runs_users_with_bounded_concurrency(monkeypatch):
    async def scenario():
        import app.services.backup as backup_module
        import app.services.job_worker as worker_module
        import app.services.scheduler as scheduler

        two_started = asyncio.Event()
        release = asyncio.Event()
        active = 0
        peak_active = 0
        started = 0

        async def active_users():
            return [
                {"id": "1", "email": "one@example.com", "db_filename": "one.db"},
                {"id": "2", "email": "two@example.com", "db_filename": "two.db"},
                {"id": "3", "email": "three@example.com", "db_filename": "three.db"},
            ]

        async def open_db(filename):
            return filename

        async def no_op(*_args, **_kwargs):
            return None

        async def run_jobs(_db, user_id, _db_filename, *, worker_id):
            nonlocal active, peak_active, started
            assert worker_id == scheduler.WORKER_ID
            assert user_id in {"1", "2", "3"}
            active += 1
            started += 1
            peak_active = max(peak_active, active)
            if started == 2:
                two_started.set()
            try:
                await release.wait()
            finally:
                active -= 1

        monkeypatch.setattr(scheduler, "USERS_CONCURRENT_MAX", 2)
        monkeypatch.setattr(scheduler, "_user_offset", 0)
        monkeypatch.setattr(scheduler, "get_active_users", active_users)
        monkeypatch.setattr(scheduler, "open_user_db", open_db)
        monkeypatch.setattr(scheduler, "close_user_db", no_op)
        monkeypatch.setattr(scheduler, "_check_and_run", no_op)
        monkeypatch.setattr(worker_module, "run_jobs_for_user", run_jobs)
        monkeypatch.setattr(backup_module, "maybe_backup_central", no_op)

        tick = asyncio.create_task(scheduler.scheduler_tick())
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert tick.done() is False
        assert (started, peak_active) == (2, 2)
        release.set()
        await asyncio.wait_for(tick, timeout=1)
        return started, peak_active

    assert asyncio.run(scenario()) == (3, 2)
