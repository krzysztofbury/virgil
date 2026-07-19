"""Migration 015: legacy weekly-minutes experiments → general metric kinds.

Legacy shape: experiment_entries.duration_minutes, no kind/target columns on
experiment_activity_types, no builtin/archived flags on exercise_library.
015 must backfill value from duration_minutes, drop the old column, and mark
every pre-existing exercise_library row as builtin — idempotently.
"""

import asyncio
import importlib


async def _legacy_db(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE experiment_activity_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#3b82f6',
            display_order INTEGER DEFAULT 0,
            source_match TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute(
        """CREATE TABLE experiment_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            activity_type_id INTEGER NOT NULL,
            duration_minutes INTEGER NOT NULL DEFAULT 0,
            notes TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            source_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            UNIQUE(category, name)
        )"""
    )
    await db.execute("INSERT INTO experiment_activity_types (experiment_id, name) VALUES (1, 'Zone 2')")
    await db.execute(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, duration_minutes) "
        "VALUES (1, '2026-07-01', 1, 45)"
    )
    await db.execute("INSERT INTO exercise_library (category, section, name) VALUES ('Cardio', 'Cardio', 'Jump Rope')")
    await db.commit()
    return db


def test_migration_015_backfills_and_drops(tmp_path):
    async def scenario():
        # finally-close: an unclosed aiosqlite connection leaves a non-daemon
        # thread that hangs pytest at exit when the test body raises.
        db = await _legacy_db(tmp_path)
        try:
            mod = importlib.import_module("app.migrations.015_general_experiments")
            await mod.up(db)
            await db.commit()

            entry_cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(experiment_entries)")}
            entry = dict((await db.execute_fetchall("SELECT * FROM experiment_entries"))[0])
            at_cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(experiment_activity_types)")}
            metric = dict((await db.execute_fetchall("SELECT * FROM experiment_activity_types"))[0])
            lib = dict((await db.execute_fetchall("SELECT * FROM exercise_library"))[0])

            # Idempotent: second run must not fail or double-apply.
            await mod.up(db)
            await db.commit()
            entry_after = dict((await db.execute_fetchall("SELECT * FROM experiment_entries"))[0])
            return entry_cols, entry, at_cols, metric, lib, entry_after
        finally:
            await db.close()

    entry_cols, entry, at_cols, metric, lib, entry_after = asyncio.run(scenario())

    assert "value" in entry_cols
    assert "duration_minutes" not in entry_cols
    assert entry["value"] == 45
    assert {"kind", "target_value", "target_period"} <= at_cols
    assert metric["kind"] == "duration"
    assert metric["target_value"] == 0
    assert metric["target_period"] == "week"
    assert lib["builtin"] == 1
    assert lib["archived"] == 0
    assert entry_after["value"] == 45
