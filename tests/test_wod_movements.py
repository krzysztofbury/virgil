"""Resolving a parsed movement name to a training_exercises row."""

import asyncio

import aiosqlite

from app.services.wod_movements import resolve_movement


async def _db(tmp_path):
    db = await aiosqlite.connect(tmp_path / "u.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE training_exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            section TEXT NOT NULL,
            target_sets INTEGER,
            target_reps TEXT,
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            archived INTEGER NOT NULL DEFAULT 0,
            ad_hoc INTEGER NOT NULL DEFAULT 0
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
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category, name)
        )"""
    )
    await db.execute(
        "INSERT INTO exercise_library (category, section, name, metric, builtin) "
        "VALUES ('CrossFit', 'Core', 'Thruster', 'reps', 1)"
    )
    await db.execute(
        "INSERT INTO exercise_library (category, section, name, metric, builtin) "
        "VALUES ('CrossFit', 'Cardio', 'Row', 'time', 1)"
    )
    await db.execute("INSERT INTO training_exercises (name, section) VALUES ('Goblet Squat', 'Core')")
    await db.commit()
    return db


def test_creates_ad_hoc_row_inheriting_section_and_metric(tmp_path):
    async def run():
        db = await _db(tmp_path)
        ex_id = await resolve_movement(db, "Row")
        rows = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (ex_id,))
        assert rows[0]["name"] == "Row"
        assert rows[0]["section"] == "Cardio"
        assert rows[0]["metric"] == "time"
        assert rows[0]["ad_hoc"] == 1
        await db.close()

    asyncio.run(run())


def test_second_use_reuses_the_same_row(tmp_path):
    async def run():
        db = await _db(tmp_path)
        first = await resolve_movement(db, "Thruster")
        second = await resolve_movement(db, "Thruster")
        assert first == second
        rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises WHERE name = 'Thruster'")
        assert rows[0]["c"] == 1
        await db.close()

    asyncio.run(run())


def test_matches_existing_protocol_exercise_without_creating(tmp_path):
    async def run():
        db = await _db(tmp_path)
        await db.execute(
            "INSERT INTO exercise_library (category, section, name, metric, builtin) "
            "VALUES ('CrossFit', 'Core', 'Goblet Squat', 'reps', 1)"
        )
        ex_id = await resolve_movement(db, "goblet squat")
        rows = await db.execute_fetchall("SELECT ad_hoc FROM training_exercises WHERE id = ?", (ex_id,))
        assert rows[0]["ad_hoc"] == 0, "an existing protocol exercise must not be re-created as ad hoc"
        count = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises WHERE name = 'Goblet Squat'")
        assert count[0]["c"] == 1
        await db.close()

    asyncio.run(run())


def test_unknown_movement_creates_nothing(tmp_path):
    async def run():
        db = await _db(tmp_path)
        before = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises")
        assert await resolve_movement(db, "Devil Press") is None
        after = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises")
        assert after[0]["c"] == before[0]["c"]
        await db.close()

    asyncio.run(run())
