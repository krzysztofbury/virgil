"""The per-user runner stops at a failing migration and leaves it unrecorded.

Swallowing the error, or skipping the rollback, both passed the suite before:
nothing exercised a migration that fails.
"""

import asyncio
import sys
import types

import aiosqlite
import pytest


def _fake_migration(monkeypatch, name: str, up) -> tuple[int, str, str]:
    module = types.ModuleType(f"app.migrations.{name}")
    module.up = up
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return int(name[:3]), name, f"{name}.py"


async def _creates_marker(db):
    await db.execute("CREATE TABLE marker (step TEXT)")


async def _writes_then_fails(db):
    await db.execute("INSERT INTO marker (step) VALUES ('half-done')")
    raise RuntimeError("injected migration failure")


async def _later(db):
    await db.execute("INSERT INTO marker (step) VALUES ('later')")


def test_a_failing_migration_is_rolled_back_unrecorded_and_stops_the_run(tmp_path, monkeypatch):
    import app.migrations.runner as runner

    fakes = [
        _fake_migration(monkeypatch, "901_creates_marker", _creates_marker),
        _fake_migration(monkeypatch, "902_writes_then_fails", _writes_then_fails),
        _fake_migration(monkeypatch, "903_later", _later),
    ]
    monkeypatch.setattr(runner, "_discover", lambda: fakes)

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "user.db")
        db.row_factory = aiosqlite.Row
        try:
            with pytest.raises(RuntimeError, match="injected migration failure"):
                await runner.run_migrations(db)
            versions = [row["version"] for row in await db.execute_fetchall("SELECT version FROM schema_migrations")]
            steps = [row["step"] for row in await db.execute_fetchall("SELECT step FROM marker")]
            return versions, steps, db.in_transaction
        finally:
            await db.close()

    versions, steps, in_transaction = asyncio.run(scenario())

    assert versions == [901]
    assert steps == []
    assert in_transaction is False


async def _creates_table_then_fails(db):
    await db.execute("CREATE TABLE half_done (id INTEGER)")
    raise RuntimeError("injected migration failure")


async def _commits_on_its_own(db):
    await db.execute("CREATE TABLE committed_early (id INTEGER)")
    await db.commit()


@pytest.mark.parametrize(
    ("up", "error", "rolled_back"),
    [
        (_creates_table_then_fails, "injected migration failure", "half_done"),
        # The commit already happened; what matters is that 901 is not recorded.
        (_commits_on_its_own, "ended the runner's transaction", None),
    ],
    ids=["ddl-before-a-failure", "commit-inside-up"],
)
def test_a_migration_is_atomic_even_when_it_starts_with_ddl(tmp_path, monkeypatch, up, error, rolled_back):
    """Python's sqlite3 opens no transaction before DDL, so a CREATE ahead of the
    failure used to stay behind. A migration that commits itself would split."""
    import app.migrations.runner as runner

    fakes = [_fake_migration(monkeypatch, "901_atomic_probe", up)]
    monkeypatch.setattr(runner, "_discover", lambda: fakes)

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "user.db")
        db.row_factory = aiosqlite.Row
        try:
            with pytest.raises(RuntimeError, match=error):
                await runner.run_migrations(db)
            tables = {row["name"] for row in await db.execute_fetchall("SELECT name FROM sqlite_master")}
            versions = await db.execute_fetchall("SELECT version FROM schema_migrations")
            return tables, versions
        finally:
            await db.close()

    tables, versions = asyncio.run(scenario())

    assert versions == []
    assert rolled_back not in tables


def test_a_database_newer_than_the_code_is_refused_and_degrades_health(tmp_path, monkeypatch):
    """After an image rollback, old code met the newer schema and /healthz stayed green."""
    from fastapi import FastAPI

    import app.central_db as central_db
    import app.services.scheduler as scheduler
    import app.user_db as user_db
    from app.main import _start_application
    from app.migrations.runner import DatabaseNewerError, count_pending_migrations, run_migrations

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))

    async def no_op(*_args, **_kwargs):
        return None

    async def users():
        return [{"db_filename": "future.db"}]

    monkeypatch.setattr(central_db, "init_central_db", no_op)
    monkeypatch.setattr(central_db, "promote_admin_emails", no_op)
    monkeypatch.setattr(central_db, "get_all_users", users)
    monkeypatch.setattr(scheduler, "scheduler_loop", no_op)

    async def scenario():
        await user_db.create_user_db("future.db")
        db = await user_db.open_user_db("future.db")
        try:
            await db.execute("INSERT INTO schema_migrations (version, name) VALUES (999, '999_from_the_future.py')")
            await db.commit()
            for check in (count_pending_migrations, run_migrations):
                with pytest.raises(DatabaseNewerError):
                    await check(db)
        finally:
            await db.close()
        probe = FastAPI()
        task = await _start_application(probe)
        await task
        return probe.state.migration_failures

    assert asyncio.run(scenario()) == ["future.db"]
