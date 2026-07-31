"""Migration 018: training_sessions.wod_parsed (cached WOD parse result for PRG)."""

import asyncio
import importlib


async def _legacy_db(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE training_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            duration_minutes INTEGER,
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute("INSERT INTO training_sessions (date, notes) VALUES ('2026-07-30', 'existing session')")
    await db.commit()
    return db


def test_adds_wod_parsed_column_defaulting_to_null(tmp_path):
    async def run():
        db = await _legacy_db(tmp_path)
        try:
            mod = importlib.import_module("app.migrations.018_wod_parsed_cache")
            await mod.up(db)
            await db.commit()
            cols = await db.execute_fetchall("PRAGMA table_info(training_sessions)")
            names = {c["name"] for c in cols}
            assert "wod_parsed" in names
            rows = await db.execute_fetchall(
                "SELECT wod_parsed FROM training_sessions WHERE notes = 'existing session'"
            )
            assert rows[0]["wod_parsed"] is None, "existing rows must not get a fabricated parse result"
        finally:
            await db.close()

    asyncio.run(run())


def test_is_idempotent(tmp_path):
    async def run():
        db = await _legacy_db(tmp_path)
        try:
            mod = importlib.import_module("app.migrations.018_wod_parsed_cache")
            await mod.up(db)
            await mod.up(db)
            await db.commit()
            cols = await db.execute_fetchall("PRAGMA table_info(training_sessions)")
            assert sum(1 for c in cols if c["name"] == "wod_parsed") == 1, "column must not be added twice"
        finally:
            await db.close()

    asyncio.run(run())
