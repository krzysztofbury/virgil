"""Migration 027: durable per-user jobs schema and invariants."""

import asyncio
import importlib

import aiosqlite
import pytest


async def _db(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    return db


def test_jobs_schema_is_retry_safe_and_indexed(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "jobs.db")
        try:
            await migration.up(db)
            await migration.up(db)
            await db.commit()
            columns = {row["name"]: dict(row) for row in await db.execute_fetchall("PRAGMA table_info(jobs)")}
            indexes = {row["name"] for row in await db.execute_fetchall("PRAGMA index_list(jobs)")}
            cursor = await db.execute("INSERT INTO jobs (kind) VALUES ('backup')")
            await db.commit()
            row = dict((await db.execute_fetchall("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)))[0])
            return columns, indexes, row
        finally:
            await db.close()

    columns, indexes, row = asyncio.run(scenario())
    assert set(columns) == importlib.import_module("app.migrations.027_jobs")._REQUIRED_COLUMNS
    assert columns["id"]["pk"] == 1
    assert columns["payload_json"]["notnull"] == 1
    assert {"idx_jobs_idempotency", "idx_jobs_claimable", "idx_jobs_stale", "idx_jobs_recent"} <= indexes
    assert row["status"] == "queued"
    assert row["payload_json"] == "{}"
    assert row["attempts"] == 0
    assert row["max_attempts"] == 3
    assert row["retry_policy"] == "manual"
    assert row["run_after"] and row["created_at"] and row["updated_at"]


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        (("kind",), ("",)),
        (("kind", "status"), ("x", "unknown")),
        (("kind", "attempts", "max_attempts"), ("x", -1, 1)),
        (("kind", "attempts", "max_attempts"), ("x", 2, 1)),
        (("kind", "attempts", "max_attempts"), ("x", 1, 1)),
        (("kind", "status"), ("x", "running")),
        (("kind", "status"), ("x", "succeeded")),
        (("kind", "locked_by"), ("x", "worker-a")),
        (("kind", "locked_at"), ("x", "2026-08-29 10:00:00.000000")),
        (("kind", "claim_token"), ("x", "0" * 32)),
    ],
)
def test_jobs_schema_rejects_invalid_states(tmp_path, columns, values):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "invalid.db")
        try:
            await migration.up(db)
            placeholders = ", ".join("?" for _ in values)
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute(f"INSERT INTO jobs ({', '.join(columns)}) VALUES ({placeholders})", values)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_idempotency_index_allows_null_and_rejects_duplicate_non_null(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "idempotency.db")
        try:
            await migration.up(db)
            await db.execute("INSERT INTO jobs (kind) VALUES ('backup')")
            await db.execute("INSERT INTO jobs (kind) VALUES ('backup')")
            await db.execute("INSERT INTO jobs (kind, idempotency_key) VALUES ('backup', 'backup:1')")
            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute("INSERT INTO jobs (kind, idempotency_key) VALUES ('export', 'backup:1')")
        finally:
            await db.close()

    asyncio.run(scenario())


def test_incompatible_preexisting_jobs_table_is_refused(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "incompatible.db")
        try:
            await db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY)")
            await db.commit()
            with pytest.raises(RuntimeError, match="incompatible schema"):
                await migration.up(db)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_same_named_incompatible_index_is_refused(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "bad-index.db")
        try:
            await migration.up(db)
            await db.execute("DROP INDEX idx_jobs_claimable")
            await db.execute("CREATE INDEX idx_jobs_claimable ON jobs(id)")
            with pytest.raises(RuntimeError, match="indexes have incompatible definitions"):
                await migration.up(db)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_schema_validation_preserves_literal_case(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / "literal-case.db")
        try:
            altered = migration._CREATE_JOBS_SQL.replace("'queued'", "'QUEUED'", 1)
            await db.execute(altered)
            with pytest.raises(RuntimeError, match="incompatible definition"):
                await migration.up(db)
        finally:
            await db.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("completed_indexes", range(5))
def test_migration_recovers_after_each_ddl_boundary(tmp_path, completed_indexes):
    async def scenario():
        migration = importlib.import_module("app.migrations.027_jobs")
        db = await _db(tmp_path / f"interrupted-{completed_indexes}.db")
        try:
            await db.execute(migration._CREATE_JOBS_SQL)
            for statement in list(migration._INDEX_SQL.values())[:completed_indexes]:
                await db.execute(statement)
            await db.commit()

            await migration.up(db)
            indexes = {row["name"] for row in await db.execute_fetchall("PRAGMA index_list(jobs)")}
            return indexes
        finally:
            await db.close()

    assert set(importlib.import_module("app.migrations.027_jobs")._INDEX_SQL) <= asyncio.run(scenario())


def test_full_migration_chain_records_027(tmp_path):
    async def scenario():
        from app.migrations.runner import run_migrations

        db = await _db(tmp_path / "fresh.db")
        try:
            await run_migrations(db)
            marker = await db.execute_fetchall(
                "SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1"
            )
            jobs = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'jobs'")
            return tuple(marker[0]), bool(jobs)
        finally:
            await db.close()

    marker, has_jobs = asyncio.run(scenario())
    assert marker == (27, "027_jobs.py")
    assert has_jobs is True
