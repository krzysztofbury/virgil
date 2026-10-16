"""Migration 034: the No Porn module becomes Freedom's Porn habit.

Runs on the supported upgrade path: a database migrated to 033 with real
No Porn history, and a fresh one that never used the module.
"""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


async def _db_at_033(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
        if int(re.match(r"\d{3}", file.name).group()) > 33:
            continue
        await importlib.import_module(f"app.migrations.{file.stem}").up(db)
        await db.commit()
    return db


def _migration():
    return importlib.import_module("app.migrations.034_freedom_habits")


def test_no_porn_history_moves_onto_a_porn_habit(tmp_path):
    async def scenario():
        db = await _db_at_033(tmp_path / "upgrade.db")
        try:
            await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('feature_no_porn', '1')")
            await db.execute(
                "UPDATE feniks_config SET start_date = '2026-05-01', target_days = 120, big_why = 'be present'"
            )
            await db.execute(
                "INSERT INTO feniks_daily (date, used, minutes, edging, note) VALUES "
                "('2026-08-01', 1, 30, 1, 'late night'), ('2026-08-02', 0, NULL, 0, 'brief, under five')"
            )
            await db.execute(
                "INSERT INTO feniks_bricks (date, hook, craving, story) VALUES ('2026-08-03', 'walked', 7, '')"
            )
            await db.execute(
                "INSERT INTO pmo_events (date, event_type, notes) VALUES "
                "('2026-08-01', 'relapse', 'via daily log'), ('2026-07-20', 'relapse', 'recorded by hand')"
            )
            await db.commit()

            await _migration().up(db)
            await db.commit()

            habit = dict((await db.execute_fetchall("SELECT * FROM habits WHERE slug = 'porn'"))[0])
            days = await db.execute_fetchall(
                "SELECT habit_id, date, slipped, amount, near_miss, note FROM feniks_daily ORDER BY date"
            )
            bricks = await db.execute_fetchall("SELECT habit_id, hook, craving FROM feniks_bricks")
            events = await db.execute_fetchall("SELECT habit_id, date, notes FROM pmo_events ORDER BY date")
            flags = await db.execute_fetchall("SELECT key, value FROM app_settings WHERE key LIKE 'feature_%'")
            return habit, days, bricks, events, flags
        finally:
            await db.close()

    habit, days, bricks, events, flags = asyncio.run(scenario())
    assert (habit["start_date"], habit["target_days"], habit["big_why"]) == ("2026-05-01", 120, "be present")
    assert (habit["slip_label"], habit["amount_unit"], habit["slip_threshold"], habit["near_miss_label"]) == (
        "I watched",
        "minutes",
        5,
        "Edging",
    )
    porn = habit["id"]
    assert [tuple(d) for d in days] == [
        (porn, "2026-08-01", 1, 30, 1, "late night"),
        (porn, "2026-08-02", 0, None, 0, "brief, under five"),
    ]
    assert [tuple(b) for b in bricks] == [(porn, "walked", 7)]
    assert [tuple(e) for e in events] == [
        (porn, "2026-07-20", "recorded by hand"),
        (porn, "2026-08-01", "via daily log"),
    ]
    assert {tuple(f) for f in flags} >= {("feature_freedom", "1")}
    assert "feature_no_porn" not in {f[0] for f in flags}


def test_an_account_that_never_used_the_module_gets_no_habit(tmp_path):
    async def scenario():
        db = await _db_at_033(tmp_path / "fresh.db")
        try:
            await _migration().up(db)
            await db.commit()
            # One day per habit: two habits may log the same date.
            await db.execute("INSERT INTO habits (slug, name, start_date) VALUES ('a', 'A', '2026-01-01')")
            await db.execute("INSERT INTO habits (slug, name, start_date) VALUES ('b', 'B', '2026-01-01')")
            await db.execute("INSERT INTO feniks_daily (habit_id, date) VALUES (1, '2026-08-01'), (2, '2026-08-01')")
            return await db.execute_fetchall("SELECT slug FROM habits WHERE slug = 'porn'")
        finally:
            await db.close()

    assert asyncio.run(scenario()) == []


def test_a_crashed_run_can_be_repeated(tmp_path):
    """Each step is gated on its own marker, so running it again changes nothing."""

    async def scenario():
        db = await _db_at_033(tmp_path / "retry.db")
        try:
            await db.execute("INSERT INTO feniks_daily (date, used, minutes) VALUES ('2026-08-01', 1, 10)")
            await db.commit()
            await _migration().up(db)
            await db.commit()
            await _migration().up(db)
            await db.commit()
            return (
                await db.execute_fetchall("SELECT COUNT(*) AS c FROM habits"),
                await db.execute_fetchall("SELECT COUNT(*) AS c FROM feniks_daily"),
            )
        finally:
            await db.close()

    habits, days = asyncio.run(scenario())
    assert (habits[0]["c"], days[0]["c"]) == (1, 1)
