"""Migration 011 must flag weighted holds/carries as metric='time' so they log
weight+seconds instead of being multiplied as reps×weight (garbage kg-volume)."""

import asyncio
import importlib

import aiosqlite

_mod = importlib.import_module("app.migrations.011_exercise_metric")


def test_migration_classifies_time_vs_reps():
    async def run():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE training_exercises (id INTEGER PRIMARY KEY, name TEXT, section TEXT, target_reps TEXT)"
        )
        await db.execute("CREATE TABLE exercise_library (id INTEGER PRIMARY KEY, name TEXT, section TEXT, reps TEXT)")
        await db.executemany(
            "INSERT INTO training_exercises (name, section, target_reps) VALUES (?,?,?)",
            [
                ("Farmer's Walk", "Core", "30-45s"),  # ends in 's' -> time
                ("Plank", "Core", "max"),  # name-based -> time
                ("Goblet Squat", "Core", "10-12"),  # reps
                ("Bent-over Row", "Core", "10-12"),  # reps
                ("Zone 2 Walk", "Cardio", "2 min"),  # 'min' -> time
            ],
        )
        await db.execute("INSERT INTO exercise_library (name, section, reps) VALUES ('Goblet Hold', 'Core', '90s')")
        await _mod.up(db)

        te = {r["name"]: r["metric"] for r in await db.execute_fetchall("SELECT name, metric FROM training_exercises")}
        assert te["Farmer's Walk"] == "time"
        assert te["Plank"] == "time"
        assert te["Zone 2 Walk"] == "time"
        assert te["Goblet Squat"] == "reps"
        assert te["Bent-over Row"] == "reps"

        lib = {r["name"]: r["metric"] for r in await db.execute_fetchall("SELECT name, metric FROM exercise_library")}
        assert lib["Goblet Hold"] == "time"

        await db.close()

    asyncio.run(run())


def test_migration_is_idempotent():
    async def run():
        db = await aiosqlite.connect(":memory:")
        db.row_factory = aiosqlite.Row
        await db.execute(
            "CREATE TABLE training_exercises (id INTEGER PRIMARY KEY, name TEXT, section TEXT, target_reps TEXT)"
        )
        await db.execute("CREATE TABLE exercise_library (id INTEGER PRIMARY KEY, name TEXT, section TEXT, reps TEXT)")
        await db.execute("INSERT INTO training_exercises (name, section, target_reps) VALUES ('Plank','Core','max')")
        await _mod.up(db)
        await _mod.up(db)  # second run must not raise (column already exists)
        rows = await db.execute_fetchall("SELECT metric FROM training_exercises WHERE name='Plank'")
        assert rows[0]["metric"] == "time"
        await db.close()

    asyncio.run(run())
