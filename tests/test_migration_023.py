"""Migration 023: reconcile No Porn tables shipped by migration 022 (released on
main before the single-flow redesign) with the redesigned shape — feniks_daily
gains note, feniks_bricks rebuilds from situation/action/lesson to story."""

import asyncio
import importlib


async def _db_after_old_022(tmp_path):
    """A DB exactly as released migration 022 leaves it: old-shape tables + data."""
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "old022.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE feniks_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
            minutes INTEGER,
            edging INTEGER NOT NULL DEFAULT 0 CHECK(edging IN (0, 1)),
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE feniks_bricks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            hook TEXT NOT NULL,
            situation TEXT DEFAULT '',
            craving INTEGER CHECK(craving BETWEEN 0 AND 10),
            action TEXT DEFAULT '',
            lesson TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute("INSERT INTO feniks_daily (date, used, minutes, edging) VALUES ('2026-08-22', 1, 35, 1)")
    await db.execute(
        "INSERT INTO feniks_bricks (date, hook, situation, craving, action, lesson) "
        "VALUES ('2026-08-22', 'Walked it off', 'alone at home', 7, 'went outside', 'urge passed')"
    )
    await db.commit()
    return db


def test_reconciles_old_022_shape_preserving_data(tmp_path):
    async def run():
        db = await _db_after_old_022(tmp_path)
        try:
            mod = importlib.import_module("app.migrations.023_feniks_single_flow")
            await mod.up(db)
            await db.commit()

            cols = {c["name"] for c in await db.execute_fetchall("PRAGMA table_info(feniks_daily)")}
            assert "note" in cols
            row = (await db.execute_fetchall("SELECT used, minutes, edging, note FROM feniks_daily"))[0]
            assert (row["used"], row["minutes"], row["edging"], row["note"]) == (1, 35, 1, "")

            cols = {c["name"] for c in await db.execute_fetchall("PRAGMA table_info(feniks_bricks)")}
            assert "story" in cols
            assert {"situation", "action", "lesson"}.isdisjoint(cols)
            brick = (await db.execute_fetchall("SELECT hook, craving, story FROM feniks_bricks"))[0]
            assert brick["hook"] == "Walked it off"
            assert brick["craving"] == 7
            for fragment in ("alone at home", "went outside", "urge passed"):
                assert fragment in brick["story"], "old free-text fields must merge into story, not vanish"
        finally:
            await db.close()

    asyncio.run(run())


def test_idempotent_and_noop_on_new_shape(tmp_path):
    """Running twice, or on a fresh DB already created in the new shape
    (app/db.py), must change nothing and raise nothing."""

    async def run():
        db = await _db_after_old_022(tmp_path)
        try:
            mod = importlib.import_module("app.migrations.023_feniks_single_flow")
            await mod.up(db)
            await db.commit()
            before = await db.execute_fetchall("SELECT hook, craving, story FROM feniks_bricks")
            await mod.up(db)
            await db.commit()
            after = await db.execute_fetchall("SELECT hook, craving, story FROM feniks_bricks")
            assert [tuple(r) for r in before] == [tuple(r) for r in after]
        finally:
            await db.close()

    asyncio.run(run())
