"""Migration 021 backfills Air Squat into the closed WOD vocabulary.

Reported bug: the note "amrap 20 minutes, 7 series, 5x pull up, 10x push ups,
15 squats" - Cindy - had no canonical match for "15 squats". Migration 016
seeded Back, Front and Overhead Squat but no bodyweight squat, and the parser's
prompt forbids guessing a near match, so a third of the workout could only come
back as `unmatched`.
"""

import asyncio
import importlib

import aiosqlite

from app.services.wod_parser import canonical_movements

# A module name cannot start with a digit, so the migration is only reachable
# through importlib - the same way every other migration test loads its subject.
m021 = importlib.import_module("app.migrations.021_air_squat")

_POST_019_SCHEMA = """
    CREATE TABLE exercise_library (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        section TEXT NOT NULL,
        name TEXT NOT NULL,
        sets INTEGER,
        reps TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        display_order INTEGER DEFAULT 0,
        metric TEXT NOT NULL DEFAULT 'reps',
        builtin INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        UNIQUE(name COLLATE NOCASE)
    )
"""

# The pre-019 shape migration 016 itself writes against, `category` and all.
_PRE_019_SCHEMA = """
    CREATE TABLE exercise_library (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        section TEXT NOT NULL,
        name TEXT NOT NULL,
        sets INTEGER,
        reps TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        display_order INTEGER DEFAULT 0,
        metric TEXT NOT NULL DEFAULT 'reps',
        builtin INTEGER NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0,
        UNIQUE(category, name)
    )
"""


async def _db(tmp_path, schema, name="lib.db"):
    db = await aiosqlite.connect(tmp_path / name)
    db.row_factory = aiosqlite.Row
    await db.execute(schema)
    await db.commit()
    return db


def test_air_squat_is_added_to_a_post_019_library(tmp_path):
    async def run():
        db = await _db(tmp_path, _POST_019_SCHEMA)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Core', 'Back Squat', 23, 'reps')"
            )
            await db.commit()
            before = [m["name"] for m in await canonical_movements(db)]
            assert "Air Squat" not in before, "the pre-state must really lack it"

            await m021.up(db)
            await db.commit()

            movements = {m["name"]: m for m in await canonical_movements(db)}
            assert "Air Squat" in movements, "the parser vocabulary must now offer a bodyweight squat"
            assert movements["Air Squat"]["metric"] == "reps", "reps, not time - it is counted, never held"
            assert movements["Air Squat"]["section"] == "Core"

        finally:
            await db.close()

    asyncio.run(run())


def test_air_squat_is_added_to_a_pre_019_library(tmp_path):
    """019 drops `category` but 016 still writes it, so this migration must be
    correct on either shape rather than depending on migration ordering."""

    async def run():
        db = await _db(tmp_path, _PRE_019_SCHEMA)
        try:
            await m021.up(db)
            await db.commit()
            rows = await db.execute_fetchall(
                "SELECT category, section, metric FROM exercise_library WHERE name = 'Air Squat'"
            )
            assert len(rows) == 1
            assert rows[0]["category"] == "CrossFit"
            assert rows[0]["section"] == "Core"
        finally:
            await db.close()

    asyncio.run(run())


def test_rerunning_does_not_duplicate_or_overwrite(tmp_path):
    """INSERT OR IGNORE: a second run is a no-op, and a user who added the
    movement themselves keeps their own metric/section choices."""

    async def run():
        db = await _db(tmp_path, _POST_019_SCHEMA)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Cardio', 'air squat', 5, 'time')"
            )
            await db.commit()

            await m021.up(db)
            await m021.up(db)
            await db.commit()

            rows = await db.execute_fetchall(
                "SELECT section, metric, display_order FROM exercise_library WHERE lower(name) = 'air squat'"
            )
            assert len(rows) == 1, "UNIQUE(name COLLATE NOCASE) plus OR IGNORE must keep this at one row"
            assert rows[0]["section"] == "Cardio", "the user's own row must not be overwritten"
            assert rows[0]["metric"] == "time"
            assert rows[0]["display_order"] == 5
        finally:
            await db.close()

    asyncio.run(run())


def test_missing_exercise_library_is_tolerated(tmp_path):
    """A database predating 009 has no vocabulary to extend; the migration must
    return quietly rather than raise and block every later migration."""

    async def run():
        db = await aiosqlite.connect(tmp_path / "bare.db")
        db.row_factory = aiosqlite.Row
        try:
            await m021.up(db)
        finally:
            await db.close()

    asyncio.run(run())


def test_full_migration_chain_yields_air_squat(tmp_path):
    """End-to-end through the real runner: a fresh install must reach the same
    vocabulary as an upgraded one. 016 now seeds Air Squat and 021 no-ops."""

    async def run():
        from app.migrations.runner import run_migrations

        db = await aiosqlite.connect(tmp_path / "fresh.db")
        db.row_factory = aiosqlite.Row
        try:
            await run_migrations(db)
            movements = {m["name"] for m in await canonical_movements(db)}
            assert "Air Squat" in movements
            # The loaded variants must survive alongside it, not be replaced.
            assert {"Back Squat", "Front Squat", "Overhead Squat"} <= movements
            rows = await db.execute_fetchall(
                "SELECT COUNT(*) AS c FROM exercise_library WHERE lower(name) = 'air squat'"
            )
            assert rows[0]["c"] == 1, "016 and 021 must not both insert it"
        finally:
            await db.close()

    asyncio.run(run())
