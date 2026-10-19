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
    monkeypatch.setattr(runner, "_discover_pending", lambda current: [m for m in fakes if m[0] > current])

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
