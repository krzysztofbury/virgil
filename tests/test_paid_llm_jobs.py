import asyncio
import importlib

import aiosqlite
import litellm
import pytest

from app.services.job_producers import ActiveWorkloadConflictError
from app.services.jobs import claim_next_job, complete_job
from app.services.llm import LLMCallAmbiguousError, call_llm
from app.services.llm_jobs import (
    enqueue_paid_llm_job,
    llm_result_published,
    record_llm_publication,
)


async def _db(path):
    from app.migrations.runner import run_migrations

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await run_migrations(db)
    return db


@pytest.mark.parametrize(
    "error",
    [
        litellm.Timeout("timed out", "test/model", "test"),
        litellm.APIError(500, "provider failed", "test", "test/model"),
    ],
)
def test_provider_uncertainty_has_a_distinct_error(tmp_path, monkeypatch, error):
    async def fail(**kwargs):
        raise error

    async def scenario():
        db = await _db(tmp_path / "uncertain.db")
        try:
            monkeypatch.setattr("app.services.llm._resolve_provider", lambda db: _provider())
            monkeypatch.setattr(litellm, "acompletion", fail)
            with pytest.raises(LLMCallAmbiguousError):
                await call_llm(db, "system", "user")
        finally:
            await db.close()

    async def _provider():
        return "test/model", "secret"

    asyncio.run(scenario())


def test_paid_llm_jobs_are_manual_single_attempt_and_queue_bounded(tmp_path):
    async def scenario():
        db = await _db(tmp_path / "jobs.db")
        try:
            first = await enqueue_paid_llm_job(
                db,
                "morning_briefing",
                {"date": "2026-08-30"},
                idempotency_key="morning_briefing:2026-08-30",
            )
            duplicate = await enqueue_paid_llm_job(
                db,
                "morning_briefing",
                {"date": "2026-08-30"},
                idempotency_key="morning_briefing:2026-08-30",
            )
            with pytest.raises(ActiveWorkloadConflictError):
                await enqueue_paid_llm_job(
                    db,
                    "morning_briefing",
                    {"date": "2026-08-31"},
                    idempotency_key="morning_briefing:2026-08-31",
                )
            row = (await db.execute_fetchall("SELECT * FROM jobs WHERE id = ?", (first.job_id,)))[0]
            return first, duplicate, row
        finally:
            await db.close()

    first, duplicate, row = asyncio.run(scenario())
    assert first.created is True
    assert duplicate == type(first)(job_id=first.job_id, created=False)
    assert row["max_attempts"] == 1
    assert row["retry_policy"] == "manual"


def test_publication_marker_survives_job_pruning_and_rolls_back_with_domain_write(tmp_path):
    async def scenario():
        db = await _db(tmp_path / "publication.db")
        try:
            queued = await enqueue_paid_llm_job(
                db,
                "wod_parse",
                {"capture_id": 7},
                idempotency_key="wod_parse:7",
            )
            claimed = await claim_next_job(db, "worker")
            assert claimed is not None
            await db.execute("BEGIN")
            await record_llm_publication(db, "wod_parse", "wod_parse:7", queued.job_id)
            await db.rollback()
            rolled_back = await llm_result_published(db, "wod_parse", "wod_parse:7")

            await db.execute("BEGIN")
            await record_llm_publication(db, "wod_parse", "wod_parse:7", queued.job_id)
            await db.commit()
            await complete_job(db, queued.job_id, claimed["claim_token"], {"parsed": True})
            await db.execute("DELETE FROM jobs WHERE id = ?", (queued.job_id,))
            await db.commit()
            durable = await llm_result_published(db, "wod_parse", "wod_parse:7")
            return rolled_back, durable
        finally:
            await db.close()

    assert asyncio.run(scenario()) == (False, True)


def test_migration_029_rejects_wrong_existing_index(tmp_path):
    async def scenario():
        jobs_migration = importlib.import_module("app.migrations.027_jobs")
        migration = importlib.import_module("app.migrations.029_llm_publications")
        db = await aiosqlite.connect(tmp_path / "wrong-index.db")
        db.row_factory = aiosqlite.Row
        try:
            await jobs_migration.up(db)
            await db.execute(f"CREATE INDEX {migration._INDEX_NAME} ON jobs(kind)")
            with pytest.raises(RuntimeError, match="unsupported definition"):
                await migration.up(db)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_migration_029_index_matches_the_live_kind_tuple():
    """A new paid kind needs a new migration, not an edited historical one."""
    from app.services.llm_jobs import PAID_LLM_JOB_KINDS

    migration = importlib.import_module("app.migrations.029_llm_publications")
    assert migration._KINDS == PAID_LLM_JOB_KINDS
    assert all(f"'{kind}'" in migration._INDEX_SQL for kind in PAID_LLM_JOB_KINDS)
