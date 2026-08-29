"""Durable job state transitions across independent SQLite connections."""

import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from app.services.jobs import (
    IdempotencyConflictError,
    RecoveryResult,
    cancel_job,
    claim_next_job,
    complete_job,
    enqueue_job,
    fail_job,
    get_job,
    heartbeat_job,
    recover_stale_jobs,
    retry_job,
)


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
    await db.commit()
    return db


def test_concurrent_idempotent_enqueue_creates_one_row(tmp_path):
    async def scenario():
        path = tmp_path / "enqueue.db"
        setup = await _setup(path)
        await setup.close()
        first, second, blocker = await _open(path), await _open(path), await _open(path)
        try:
            await blocker.execute("BEGIN IMMEDIATE")
            tasks = [
                asyncio.create_task(
                    enqueue_job(first, "backup", {"scope": "user"}, idempotency_key="backup:2026-08-29")
                ),
                asyncio.create_task(
                    enqueue_job(second, "backup", {"scope": "user"}, idempotency_key="backup:2026-08-29")
                ),
            ]
            await asyncio.sleep(0)
            assert not any(task.done() for task in tasks)
            await blocker.commit()
            results = await asyncio.gather(*tasks)
            rows = await first.execute_fetchall("SELECT id FROM jobs")
            return results, rows
        finally:
            await first.close()
            await second.close()
            await blocker.close()

    results, rows = asyncio.run(scenario())
    assert sorted(result.created for result in results) == [False, True]
    assert results[0].job_id == results[1].job_id
    assert len(rows) == 1


def test_idempotency_key_reuse_with_different_work_is_loud(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "conflict.db")
        try:
            await enqueue_job(db, "backup", {"scope": "user"}, idempotency_key="slot:1")
            with pytest.raises(IdempotencyConflictError):
                await enqueue_job(db, "backup", {"scope": "central"}, idempotency_key="slot:1")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_two_workers_claim_at_most_one_job_per_user(tmp_path):
    async def scenario():
        path = tmp_path / "claim.db"
        setup = await _setup(path)
        now = datetime(2026, 8, 29, 10, tzinfo=UTC)
        await enqueue_job(setup, "backup", idempotency_key="backup:1", run_after=now)
        await enqueue_job(setup, "backup", idempotency_key="backup:2", run_after=now)
        await setup.close()
        first, second, blocker = await _open(path), await _open(path), await _open(path)
        try:
            await blocker.execute("BEGIN IMMEDIATE")
            tasks = [
                asyncio.create_task(claim_next_job(first, "worker-a", now=now)),
                asyncio.create_task(claim_next_job(second, "worker-b", now=now)),
            ]
            await asyncio.sleep(0)
            assert not any(task.done() for task in tasks)
            await blocker.commit()
            claims = await asyncio.gather(*tasks)
            rows = await first.execute_fetchall("SELECT status, attempts FROM jobs ORDER BY id")
            return claims, [tuple(row) for row in rows]
        finally:
            await first.close()
            await second.close()
            await blocker.close()

    claims, rows = asyncio.run(scenario())
    assert sum(claim is not None for claim in claims) == 1
    assert rows == [("running", 1), ("queued", 0)]


def test_claim_commit_releases_foreground_writer(tmp_path):
    async def scenario():
        path = tmp_path / "foreground.db"
        worker = await _setup(path)
        await worker.execute("CREATE TABLE foreground_writes (value TEXT NOT NULL)")
        await worker.commit()
        await enqueue_job(worker, "backup")
        job = await claim_next_job(worker, "worker-a")
        foreground = await _open(path)
        try:
            await asyncio.wait_for(foreground.execute("INSERT INTO foreground_writes VALUES ('saved')"), timeout=1)
            await foreground.commit()
            rows = await foreground.execute_fetchall("SELECT value FROM foreground_writes")
            return job, [row["value"] for row in rows]
        finally:
            await worker.close()
            await foreground.close()

    job, values = asyncio.run(scenario())
    assert job and job["status"] == "running"
    assert values == ["saved"]


def test_automatic_failure_retries_with_persisted_delay_then_exhausts(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "retry.db")
        start = datetime(2026, 8, 29, 10, tzinfo=UTC)
        try:
            result = await enqueue_job(db, "backup", max_attempts=2, retry_policy="automatic", run_after=start)
            first = await claim_next_job(db, "worker-a", now=start)
            first_status = await fail_job(
                db, first["id"], first["claim_token"], "Temporary failure", retry_delay_seconds=30, now=start
            )
            too_early = await claim_next_job(db, "worker-b", now=start + timedelta(seconds=29))
            second = await claim_next_job(db, "worker-b", now=start + timedelta(seconds=30))
            second_status = await fail_job(
                db,
                second["id"],
                second["claim_token"],
                "Still unavailable",
                retry_delay_seconds=30,
                now=start + timedelta(seconds=30),
            )
            return result.job_id, first_status, too_early, second_status, await get_job(db, result.job_id)
        finally:
            await db.close()

    job_id, first_status, too_early, second_status, job = asyncio.run(scenario())
    assert job["id"] == job_id
    assert first_status == "queued"
    assert too_early is None
    assert second_status == "failed"
    assert job["attempts"] == job["max_attempts"] == 2
    assert job["finished_at"] is not None
    assert job["locked_by"] is None


def test_stale_paid_job_needs_attention_and_only_explicit_retry_requeues(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "paid.db")
        started = datetime(2026, 8, 29, 10, tzinfo=UTC)
        try:
            result = await enqueue_job(
                db,
                "briefing",
                {"date": "2026-08-29"},
                max_attempts=1,
                retry_policy="manual",
                run_after=started,
            )
            claimed = await claim_next_job(db, "dead-worker", now=started)
            recovered = await recover_stale_jobs(
                db,
                started + timedelta(seconds=1),
                now=started + timedelta(minutes=10),
            )
            before_retry = await get_job(db, result.job_id)
            retried = await retry_job(db, result.job_id, now=started + timedelta(minutes=11))
            claimed_again = await claim_next_job(db, "dead-worker", now=started + timedelta(minutes=11))
            completion_time = started + timedelta(minutes=11)
            stale_completion = await complete_job(
                db, result.job_id, claimed["claim_token"], {"stale": True}, now=completion_time
            )
            current_completion = await complete_job(
                db, result.job_id, claimed_again["claim_token"], {"ok": True}, now=completion_time
            )
            return claimed, recovered, before_retry, retried, claimed_again, stale_completion, current_completion
        finally:
            await db.close()

    claimed, recovered, before_retry, retried, claimed_again, stale_completion, current_completion = asyncio.run(
        scenario()
    )
    assert claimed["attempts"] == 1
    assert recovered == [RecoveryResult(claimed["id"], "needs_attention")]
    assert before_retry["status"] == "needs_attention"
    assert before_retry["finished_at"] is not None
    assert retried is True
    assert claimed_again["attempts"] == 2
    assert claimed_again["max_attempts"] == 2
    assert stale_completion is False
    assert current_completion is True


def test_owner_checks_heartbeat_and_completion(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "ownership.db")
        try:
            result = await enqueue_job(db, "backup")
            claimed = await claim_next_job(db, "worker-a")
            wrong_token = "0" * 32
            wrong_heartbeat = await heartbeat_job(db, result.job_id, wrong_token)
            right_heartbeat = await heartbeat_job(db, result.job_id, claimed["claim_token"])
            wrong_complete = await complete_job(db, result.job_id, wrong_token, {"ok": True})
            right_complete = await complete_job(db, result.job_id, claimed["claim_token"], {"ok": True})
            return wrong_heartbeat, right_heartbeat, wrong_complete, right_complete, await get_job(db, result.job_id)
        finally:
            await db.close()

    wrong_heartbeat, right_heartbeat, wrong_complete, right_complete, job = asyncio.run(scenario())
    assert (wrong_heartbeat, right_heartbeat) == (False, True)
    assert (wrong_complete, right_complete) == (False, True)
    assert job["status"] == "succeeded"
    assert job["result_json"] == '{"ok":true}'


def test_cancel_only_queued_and_active_transaction_is_refused(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "cancel.db")
        try:
            queued = await enqueue_job(db, "backup")
            cancelled = await cancel_job(db, queued.job_id)
            cancelled_twice = await cancel_job(db, queued.job_id)
            await db.execute("CREATE TABLE unrelated (value TEXT)")
            await db.execute("INSERT INTO unrelated VALUES ('pending')")
            with pytest.raises(RuntimeError, match="no active transaction"):
                await enqueue_job(db, "backup")
            await db.rollback()
            return cancelled, cancelled_twice, await get_job(db, queued.job_id)
        finally:
            await db.close()

    cancelled, cancelled_twice, job = asyncio.run(scenario())
    assert (cancelled, cancelled_twice) == (True, False)
    assert job["status"] == "cancelled"


def test_input_and_output_bounds_fail_before_state_changes(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "bounds.db")
        try:
            with pytest.raises(ValueError, match="kind"):
                await enqueue_job(db, "Bad Kind")
            with pytest.raises(ValueError, match="payload"):
                await enqueue_job(db, "backup", {"value": "x" * 17000})
            with pytest.raises(ValueError, match="payload"):
                await enqueue_job(db, "backup", {"x" * 17000: True})
            with pytest.raises(ValueError, match="too complex"):
                await enqueue_job(db, "backup", {"values": [0] * 1024})
            with pytest.raises(ValueError, match="non-finite"):
                await enqueue_job(db, "backup", {"value": float("nan")})
            with pytest.raises(ValueError, match="max_attempts"):
                await enqueue_job(db, "backup", max_attempts=0)
            with pytest.raises(ValueError, match="timezone-aware"):
                await enqueue_job(db, "backup", run_after=datetime(2026, 8, 29))

            result = await enqueue_job(db, "backup")
            claimed = await claim_next_job(db, "worker-a")
            with pytest.raises(ValueError, match="result"):
                await complete_job(db, result.job_id, claimed["claim_token"], {"value": "x" * 17000})
            still_running = await get_job(db, result.job_id)
            with pytest.raises(ValueError, match="Recovery limit"):
                await recover_stale_jobs(db, datetime.now(UTC), limit=101)
            return still_running
        finally:
            await db.close()

    assert asyncio.run(scenario())["status"] == "running"


def test_failure_error_is_bounded_and_normalized(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "error.db")
        try:
            result = await enqueue_job(db, "backup", max_attempts=1)
            claimed = await claim_next_job(db, "worker-a")
            await fail_job(db, result.job_id, claimed["claim_token"], " secret\n" + "x" * 1000)
            return await get_job(db, result.job_id)
        finally:
            await db.close()

    job = asyncio.run(scenario())
    assert job["status"] == "failed"
    assert len(job["last_error"]) == 500
    assert "\n" not in job["last_error"]


def test_timestamp_precision_and_lease_time_are_monotonic(tmp_path):
    async def scenario():
        db = await _setup(tmp_path / "timestamps.db")
        due = datetime(2026, 8, 29, 10, 0, 0, 900000, tzinfo=UTC)
        try:
            result = await enqueue_job(db, "backup", run_after=due)
            too_early = await claim_next_job(db, "worker-a", now=due - timedelta(microseconds=1))
            claimed = await claim_next_job(db, "worker-a", now=due)
            regressed = await heartbeat_job(
                db, result.job_id, claimed["claim_token"], now=due - timedelta(microseconds=1)
            )
            advanced = await heartbeat_job(db, result.job_id, claimed["claim_token"], now=due + timedelta(seconds=1))
            regressed_completion = await complete_job(db, result.job_id, claimed["claim_token"], now=due)
            with pytest.raises(ValueError, match="later than now"):
                await recover_stale_jobs(db, due + timedelta(seconds=2), now=due + timedelta(seconds=1))
            with pytest.raises(ValueError, match="clock time"):
                await heartbeat_job(
                    db,
                    result.job_id,
                    claimed["claim_token"],
                    now=datetime.now(UTC) + timedelta(seconds=301),
                )
            return too_early, claimed, regressed, advanced, regressed_completion, await get_job(db, result.job_id)
        finally:
            await db.close()

    too_early, claimed, regressed, advanced, regressed_completion, job = asyncio.run(scenario())
    assert too_early is None
    assert claimed["run_after"].endswith(".900000")
    assert (regressed, advanced) == (False, True)
    assert regressed_completion is False
    assert job["locked_at"] == "2026-08-29 10:00:01.900000"


def test_mixed_job_and_foreground_write_load_converges(tmp_path):
    async def scenario():
        path = tmp_path / "mixed.db"
        setup = await _setup(path)
        await setup.execute("CREATE TABLE foreground_writes (value INTEGER NOT NULL)")
        await setup.commit()
        for index in range(20):
            await enqueue_job(setup, "backup", {"index": index})
        await setup.close()

        worker, foreground = await _open(path), await _open(path)

        async def run_jobs():
            completed = 0
            while completed < 20:
                job = await claim_next_job(worker, "worker-a")
                assert job is not None
                assert await complete_job(worker, job["id"], job["claim_token"], {"done": True})
                completed += 1

        async def run_foreground_writes():
            for index in range(50):
                await foreground.execute("INSERT INTO foreground_writes VALUES (?)", (index,))
                await foreground.commit()

        try:
            await asyncio.wait_for(asyncio.gather(run_jobs(), run_foreground_writes()), timeout=5)
            jobs = (await worker.execute_fetchall("SELECT COUNT(*) AS n FROM jobs WHERE status = 'succeeded'"))[0]["n"]
            writes = (await foreground.execute_fetchall("SELECT COUNT(*) AS n FROM foreground_writes"))[0]["n"]
            return jobs, writes
        finally:
            await worker.close()
            await foreground.close()

    assert asyncio.run(scenario()) == (20, 50)
