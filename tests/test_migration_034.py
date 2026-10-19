"""Migration 034: the No Porn module becomes Freedom's Porn habit.

Runs on the supported upgrade path: a database migrated to 033 with real
No Porn history, and a fresh one that never used the module.
"""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite
import pytest

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


PARTIAL_HISTORIES = {
    "only-bricks": [
        "INSERT INTO feniks_bricks (date, hook, craving, story) VALUES ('2026-08-03', 'walked', 7, '')",
    ],
    "only-relapses": [
        "INSERT INTO pmo_events (date, event_type, notes) VALUES ('2026-07-20', 'relapse', 'by hand')",
    ],
    "flag-on-no-data": [
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('feature_no_porn', '1')",
    ],
}


@pytest.mark.parametrize("history", PARTIAL_HISTORIES)
def test_any_trace_of_the_module_gets_a_porn_habit_and_keeps_every_row(tmp_path, history):
    """The habit used to be tested only from an account with day logs. A table the
    rebuild copied without a Porn habit to point at would lose its rows."""

    async def scenario():
        db = await _db_at_033(tmp_path / "partial.db")
        try:
            for statement in PARTIAL_HISTORIES[history]:
                await db.execute(statement)
            await db.commit()
            await _migration().up(db)
            await db.commit()
            habits = await db.execute_fetchall("SELECT id FROM habits WHERE slug = 'porn'")
            owners = {
                table: [row[0] for row in await db.execute_fetchall(f"SELECT habit_id FROM {table}")]  # noqa: S608
                for table in ("feniks_daily", "feniks_bricks", "pmo_events")
            }
            return [row[0] for row in habits], owners
        finally:
            await db.close()

    habits, owners = asyncio.run(scenario())

    assert len(habits) == 1
    expected = {"feniks_daily": 0, "feniks_bricks": 0, "pmo_events": 0}
    expected.update({"only-bricks": {"feniks_bricks": 1}, "only-relapses": {"pmo_events": 1}}.get(history, {}))
    assert {table: len(rows) for table, rows in owners.items()} == expected
    assert all(owner == habits[0] for rows in owners.values() for owner in rows)


def test_a_run_that_fails_midway_rolls_back_and_succeeds_on_retry(tmp_path, monkeypatch):
    """A crash after the day log was rebuilt but before the bricks were must not
    lose rows or leave a second Porn habit when the migration runs again."""
    migration = _migration()
    real_rebuild = migration._rebuild

    async def fail_at_bricks(db, table, *args):
        if table == "feniks_bricks":
            raise RuntimeError("injected crash")
        await real_rebuild(db, table, *args)

    async def scenario():
        db = await _db_at_033(tmp_path / "crash.db")
        try:
            await db.execute("INSERT INTO feniks_daily (date, used, minutes) VALUES ('2026-08-01', 1, 10)")
            await db.execute("INSERT INTO feniks_bricks (date, hook, craving, story) VALUES ('2026-08-03', 'h', 2, '')")
            await db.commit()
            monkeypatch.setattr(migration, "_rebuild", fail_at_bricks)
            with pytest.raises(RuntimeError, match="injected crash"):
                await migration.up(db)
            await db.rollback()
            # Back at 033: the rebuilt day log and the Porn habit are gone with the transaction.
            columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(feniks_daily)")}
            assert "habit_id" not in columns
            assert await db.execute_fetchall("SELECT 1 FROM habits") == []
            monkeypatch.setattr(migration, "_rebuild", real_rebuild)
            await migration.up(db)
            await db.commit()
            return (
                [row[0] for row in await db.execute_fetchall("SELECT id FROM habits")],
                await db.execute_fetchall("SELECT habit_id, date, slipped, amount FROM feniks_daily"),
                await db.execute_fetchall("SELECT habit_id, hook FROM feniks_bricks"),
            )
        finally:
            await db.close()

    habits, days, bricks = asyncio.run(scenario())

    assert len(habits) == 1
    assert [tuple(day) for day in days] == [(habits[0], "2026-08-01", 1, 10)]
    assert [tuple(brick) for brick in bricks] == [(habits[0], "h")]
