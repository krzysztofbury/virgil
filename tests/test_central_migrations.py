"""Versioned migrations and pre-migration snapshots for the central registry."""

import asyncio
import importlib
import sqlite3
from pathlib import Path

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI


async def _open_db(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _create_unversioned_registry(path):
    from app.central_schema import CENTRAL_SCHEMA_V1

    db = await _open_db(path)
    await db.executescript(CENTRAL_SCHEMA_V1)
    await db.execute(
        """INSERT INTO users
           (id, email, password_hash, display_name, role, db_filename)
           VALUES ('11111111-1111-1111-1111-111111111111',
                   'owner@example.com', 'hash', 'Owner', 'admin', 'owner.db')"""
    )
    await db.commit()
    return db


def test_pending_discovery_does_not_modify_unversioned_database(tmp_path):
    async def run():
        from app.central_migrations.runner import count_pending_migrations

        db = await _create_unversioned_registry(tmp_path / "central.db")
        try:
            assert await count_pending_migrations(db) == 1
            tracking = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'central_schema_migrations'"
            )
            assert tracking == []
        finally:
            await db.close()

    asyncio.run(run())


def test_fresh_database_gets_baseline_and_version_record(tmp_path):
    async def run():
        from app.central_migrations.runner import run_migrations

        db = await _open_db(tmp_path / "central.db")
        try:
            await run_migrations(db)
            tables = {
                row["name"] for row in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            assert {"users", "webhook_routes", "central_schema_migrations"} <= tables
            versions = await db.execute_fetchall("SELECT version, name FROM central_schema_migrations")
            assert [(row["version"], row["name"]) for row in versions] == [(1, "001_baseline.py")]
        finally:
            await db.close()

    asyncio.run(run())


def test_unversioned_registry_upgrades_without_losing_data(tmp_path):
    async def run():
        from app.central_migrations.runner import run_migrations

        db = await _create_unversioned_registry(tmp_path / "central.db")
        try:
            await run_migrations(db)
            await run_migrations(db)
            users = await db.execute_fetchall("SELECT id, email, db_filename FROM users")
            assert [dict(row) for row in users] == [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "email": "owner@example.com",
                    "db_filename": "owner.db",
                }
            ]
            versions = await db.execute_fetchall("SELECT version FROM central_schema_migrations")
            assert [row["version"] for row in versions] == [1]
        finally:
            await db.close()

    asyncio.run(run())


def test_failed_migration_rolls_back_schema_and_version_then_retries(tmp_path, monkeypatch):
    async def run():
        from app.central_migrations.runner import run_migrations

        baseline = importlib.import_module("app.central_migrations.001_baseline")
        real_up = baseline.up

        async def failing_up(db):
            await db.execute("CREATE TABLE partial_change (id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected central migration failure")

        monkeypatch.setattr(baseline, "up", failing_up)
        db_path = tmp_path / "central.db"
        db = await _open_db(db_path)
        try:
            with pytest.raises(RuntimeError, match="injected central migration failure"):
                await run_migrations(db)
        finally:
            await db.close()

        raw = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "partial_change" not in tables
            assert "central_schema_migrations" not in tables
        finally:
            raw.close()

        monkeypatch.setattr(baseline, "up", real_up)
        reopened = await _open_db(db_path)
        try:
            await run_migrations(reopened)
            versions = await reopened.execute_fetchall("SELECT version FROM central_schema_migrations")
            assert [row["version"] for row in versions] == [1]
        finally:
            await reopened.close()

    asyncio.run(run())


def test_cancelled_migration_releases_sqlite_transaction(tmp_path, monkeypatch):
    async def run():
        from app.central_migrations.runner import run_migrations

        baseline = importlib.import_module("app.central_migrations.001_baseline")

        async def cancelled_up(db):
            await db.execute("CREATE TABLE partial_change (id INTEGER PRIMARY KEY)")
            raise asyncio.CancelledError

        monkeypatch.setattr(baseline, "up", cancelled_up)
        db_path = tmp_path / "central.db"
        db = await _open_db(db_path)
        try:
            with pytest.raises(asyncio.CancelledError):
                await run_migrations(db)
            assert db.in_transaction is False

            other = await _open_db(db_path)
            try:
                await other.execute("BEGIN IMMEDIATE")
                await other.rollback()
            finally:
                await other.close()
        finally:
            await db.close()

    asyncio.run(run())


def test_incomplete_existing_schema_is_not_stamped_as_migrated(tmp_path):
    async def run():
        from app.central_migrations.runner import run_migrations

        db_path = tmp_path / "central.db"
        db = await _open_db(db_path)
        await db.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
        await db.commit()
        try:
            with pytest.raises(RuntimeError, match="users does not match"):
                await run_migrations(db)
        finally:
            await db.close()

        raw = sqlite3.connect(db_path)
        try:
            tracking = raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'central_schema_migrations'"
            ).fetchone()
            assert tracking is None
        finally:
            raw.close()

    asyncio.run(run())


def test_database_newer_than_code_is_rejected(tmp_path):
    async def run():
        from app.central_migrations.runner import count_pending_migrations

        db = await _open_db(tmp_path / "central.db")
        await db.execute(
            """CREATE TABLE central_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )"""
        )
        await db.execute("INSERT INTO central_schema_migrations (version, name) VALUES (999, 'future.py')")
        await db.commit()
        try:
            with pytest.raises(RuntimeError, match="newer than supported"):
                await count_pending_migrations(db)
        finally:
            await db.close()

    asyncio.run(run())


def test_migration_history_must_match_discovered_files(tmp_path):
    async def run():
        from app.central_migrations.runner import count_pending_migrations, run_migrations

        db = await _open_db(tmp_path / "central.db")
        try:
            await run_migrations(db)
            await db.execute("UPDATE central_schema_migrations SET name = 'renamed.py' WHERE version = 1")
            await db.commit()
            with pytest.raises(RuntimeError, match="history does not match"):
                await count_pending_migrations(db)
        finally:
            await db.close()

    asyncio.run(run())


def test_full_column_list_without_identity_constraints_is_rejected(tmp_path):
    async def run():
        from app.central_migrations.runner import run_migrations

        db_path = tmp_path / "central.db"
        db = await _open_db(db_path)
        await db.executescript(
            """
            CREATE TABLE users (
                id TEXT, email TEXT, password_hash TEXT, display_name TEXT, role TEXT,
                db_filename TEXT, is_active INTEGER, totp_secret TEXT, totp_enabled INTEGER,
                created_at TEXT, last_login_at TEXT
            );
            CREATE TABLE webhook_routes (
                webhook_id TEXT, user_id TEXT, provider TEXT, created_at TEXT
            );
            """
        )
        await db.commit()
        try:
            with pytest.raises(RuntimeError, match="does not match migration 001"):
                await run_migrations(db)
        finally:
            await db.close()

        raw = sqlite3.connect(db_path)
        try:
            assert (
                raw.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'central_schema_migrations'"
                ).fetchone()
                is None
            )
        finally:
            raw.close()

    asyncio.run(run())


def test_partial_unique_email_index_is_rejected(tmp_path):
    async def run():
        from app.central_migrations.runner import run_migrations
        from app.central_schema import CENTRAL_SCHEMA_V1

        db = await _open_db(tmp_path / "central.db")
        weakened_schema = CENTRAL_SCHEMA_V1.replace("email TEXT UNIQUE NOT NULL", "email TEXT NOT NULL")
        await db.executescript(weakened_schema)
        await db.execute("CREATE UNIQUE INDEX users_email_active ON users(email) WHERE is_active = 1")
        await db.commit()
        try:
            with pytest.raises(RuntimeError, match="UNIQUE constraint is missing"):
                await run_migrations(db)
        finally:
            await db.close()

    asyncio.run(run())


def test_discovered_migration_versions_must_be_contiguous(tmp_path, monkeypatch):
    import app.central_migrations.runner as runner

    (tmp_path / "001_baseline.py").write_text("", encoding="utf-8")
    (tmp_path / "003_gap.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(runner, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="contiguous from 001"):
        runner._discover_migrations()


def test_init_snapshots_existing_registry_before_tracking_table(tmp_path, monkeypatch):
    async def run():
        import app.central_db as central_db
        import app.services.backup as backup_module

        db_path = tmp_path / "virgil-central.db"
        db = await _create_unversioned_registry(db_path)
        old_singleton = central_db._central_db
        central_db._central_db = db
        monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")
        try:
            await central_db.init_central_db()
        finally:
            central_db._central_db = old_singleton
            await db.close()

        snapshots = list((tmp_path / "backups" / "pre-migration").glob("virgil-central-pre-migration-v000.db"))
        assert len(snapshots) == 1
        snapshot = sqlite3.connect(snapshots[0])
        try:
            assert snapshot.execute("SELECT email FROM users").fetchone()[0] == "owner@example.com"
            tracking = snapshot.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'central_schema_migrations'"
            ).fetchone()
            assert tracking is None
        finally:
            snapshot.close()

    asyncio.run(run())


def test_interrupted_snapshot_is_not_published(tmp_path, monkeypatch):
    async def run():
        import app.services.backup as backup_module

        db = await _create_unversioned_registry(tmp_path / "virgil-central.db")
        monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

        def fail_backup(_source, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError("injected snapshot interruption")

        monkeypatch.setattr(backup_module, "_do_backup", fail_backup)
        try:
            with pytest.raises(OSError, match="snapshot interruption"):
                await backup_module.snapshot_central_before_migration(db, 0)
        finally:
            await db.close()

        directory = tmp_path / "backups" / "pre-migration"
        assert not (directory / "virgil-central-pre-migration-v000.db").exists()
        assert list(directory.glob("*.tmp")) == []

    asyncio.run(run())


def test_existing_snapshot_must_match_source_schema_before_migration(tmp_path, monkeypatch):
    async def run():
        import app.services.backup as backup_module

        db = await _create_unversioned_registry(tmp_path / "virgil-central.db")
        backup_dir = tmp_path / "backups"
        monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
        directory = backup_dir / "pre-migration"
        directory.mkdir(parents=True)
        destination = directory / "virgil-central-pre-migration-v000.db"
        unrelated = sqlite3.connect(destination)
        unrelated.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        unrelated.commit()
        unrelated.close()
        try:
            with pytest.raises(RuntimeError, match="not the expected central schema"):
                await backup_module.snapshot_central_before_migration(db, 0)
        finally:
            await db.close()

    asyncio.run(run())


def test_concurrent_central_initialization_serializes_snapshot_and_migration(tmp_path, monkeypatch):
    async def run():
        import app.services.backup as backup_module
        from app.central_db import migrate_central_db

        db_path = tmp_path / "virgil-central.db"
        seed = await _create_unversioned_registry(db_path)
        await seed.close()
        first = await _open_db(db_path)
        second = await _open_db(db_path)
        monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")
        try:
            await asyncio.gather(migrate_central_db(first), migrate_central_db(second))
            versions = await first.execute_fetchall("SELECT version, name FROM central_schema_migrations")
            assert [(row["version"], row["name"]) for row in versions] == [(1, "001_baseline.py")]
        finally:
            await first.close()
            await second.close()

        snapshots = list((tmp_path / "backups" / "pre-migration").glob("virgil-central-pre-migration-v000.db"))
        assert len(snapshots) == 1

    asyncio.run(run())


def test_lifespan_degrades_without_starting_dependents_on_central_migration_failure(monkeypatch):
    async def run():
        import app.central_db as central_db
        import app.services.scheduler as scheduler
        from app.main import lifespan

        calls = {"close": 0}

        async def fail_init():
            raise RuntimeError("injected central migration failure")

        async def unexpected_users():
            raise AssertionError("per-user migrations must not start")

        async def close():
            calls["close"] += 1

        def unexpected_scheduler():
            raise AssertionError("scheduler must not start")

        monkeypatch.setattr(central_db, "init_central_db", fail_init)
        monkeypatch.setattr(central_db, "get_all_users", unexpected_users)
        monkeypatch.setattr(central_db, "close_central_db", close)
        monkeypatch.setattr(scheduler, "scheduler_loop", unexpected_scheduler)

        probe = FastAPI()
        async with lifespan(probe):
            assert probe.state.central_migration_failure is True
            assert probe.state.migration_failures == []
        assert calls == {"close": 1}

    asyncio.run(run())


def test_real_upgrade_failure_snapshots_rolls_back_and_quarantines_app(tmp_path, monkeypatch):
    async def run():
        import app.central_db as central_db
        import app.services.backup as backup_module
        from app.main import CentralMigrationGuardMiddleware, healthz, lifespan

        baseline = importlib.import_module("app.central_migrations.001_baseline")
        real_up = baseline.up
        db_path = tmp_path / "virgil-central.db"
        db = await _create_unversioned_registry(db_path)
        old_singleton = central_db._central_db
        central_db._central_db = db
        monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

        async def failing_up(connection):
            await connection.execute("CREATE TABLE partial_change (id INTEGER PRIMARY KEY)")
            raise RuntimeError("injected central migration failure")

        monkeypatch.setattr(baseline, "up", failing_up)
        probe = FastAPI()
        probe.add_middleware(CentralMigrationGuardMiddleware, state=probe.state)
        probe.add_api_route("/healthz", healthz)

        @probe.get("/normal")
        async def normal_route():
            return {"status": "must not run"}

        try:
            async with lifespan(probe):
                transport = httpx.ASGITransport(app=probe)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    health = await client.get("/healthz")
                    assert health.status_code == 503
                    assert health.json() == {"status": "degraded"}
                    blocked = await client.get("/normal")
                    assert blocked.status_code == 503
                    assert blocked.json() == {"status": "unavailable"}
        finally:
            central_db._central_db = old_singleton

        raw = sqlite3.connect(db_path)
        try:
            tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            assert "partial_change" not in tables
            assert "central_schema_migrations" not in tables
        finally:
            raw.close()

        snapshot_path = tmp_path / "backups" / "pre-migration" / "virgil-central-pre-migration-v000.db"
        snapshot = sqlite3.connect(snapshot_path)
        try:
            assert snapshot.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert snapshot.execute("SELECT email FROM users").fetchone() == ("owner@example.com",)
        finally:
            snapshot.close()

        monkeypatch.setattr(baseline, "up", real_up)
        recovered = await _open_db(db_path)
        try:
            await central_db.migrate_central_db(recovered)
            versions = await recovered.execute_fetchall("SELECT version FROM central_schema_migrations")
            assert [row["version"] for row in versions] == [1]
        finally:
            await recovered.close()

    asyncio.run(run())
