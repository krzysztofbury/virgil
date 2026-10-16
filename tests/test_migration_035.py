"""Migration 035: the No Porn leftovers nothing reads are dropped; the person's writing stays."""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


async def _db_at_034(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
        if int(re.match(r"\d{3}", file.name).group()) > 34:
            continue
        await importlib.import_module(f"app.migrations.{file.stem}").up(db)
        await db.commit()
    return db


def test_leftovers_are_dropped_and_journal_writing_is_kept(tmp_path):
    migration = importlib.import_module("app.migrations.035_drop_no_porn_leftovers")

    async def scenario():
        db = await _db_at_034(tmp_path / "upgrade.db")
        try:
            await db.execute("INSERT INTO feniks_journal (date, emotions) VALUES ('2026-07-01', 'restless')")
            await db.execute("INSERT INTO feniks_pleasures (date, pleasure_1) VALUES ('2026-07-01', 'a long walk')")
            await db.commit()
            for _ in range(2):  # a crashed run is simply repeated
                await migration.up(db)
                await db.commit()
            tables = {
                row["name"] for row in await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            journal = await db.execute_fetchall("SELECT date, emotions FROM feniks_journal")
            pleasures = await db.execute_fetchall("SELECT date, pleasure_1 FROM feniks_pleasures")
            return tables, [tuple(r) for r in journal], [tuple(r) for r in pleasures]
        finally:
            await db.close()

    tables, journal, pleasures = asyncio.run(scenario())
    assert not {"feniks_config", "feniks_milestones"} & tables
    assert journal == [("2026-07-01", "restless")]
    assert pleasures == [("2026-07-01", "a long walk")]
