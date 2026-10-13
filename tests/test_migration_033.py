"""Migration 033: movement families, aliases, equipment and duplicate merges.

Runs on the supported upgrade path: a database migrated to 032 with real
history under the names that get merged.
"""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


async def _db_at_032(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
        if int(re.match(r"\d{3}", file.name).group()) > 32:
            continue
        await importlib.import_module(f"app.migrations.{file.stem}").up(db)
        await db.commit()
    return db


async def _session_with(db, movement: str, weight: float) -> int:
    cursor = await db.execute(
        "INSERT INTO training_exercises (name, section, metric, display_order) VALUES (?, 'Core', 'reps', 99) "
        "ON CONFLICT(name COLLATE NOCASE) DO UPDATE SET name = excluded.name RETURNING id",
        (movement,),
    )
    exercise_id = (await cursor.fetchone())[0]
    session = await db.execute("INSERT INTO training_sessions (date, notes) VALUES ('2026-08-01', ?)", (movement,))
    await db.execute(
        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, 1, 10, ?)",
        (session.lastrowid, exercise_id, weight),
    )
    return session.lastrowid


def _history(rows) -> list[tuple]:
    return sorted((row["name"], row["weight"]) for row in rows)


def test_merges_keep_history_under_the_surviving_movement(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.033_movement_taxonomy")
        db = await _db_at_032(tmp_path / "upgrade.db")
        try:
            await _session_with(db, "Kettlebell Swing", 16)
            await _session_with(db, "KB Swing", 20)
            await _session_with(db, "Shoulder Press", 40)
            await _session_with(db, "Halo (KB)", 8)
            await db.commit()

            await migration.up(db)
            await db.commit()
            await migration.up(db)  # re-running after an interruption is safe
            await db.commit()

            history = await db.execute_fetchall(
                "SELECT x.name, e.weight FROM training_entries e JOIN training_exercises x ON x.id = e.exercise_id"
            )
            library = {row["name"] for row in await db.execute_fetchall("SELECT name FROM exercise_library")}
            aliases = {
                row["alias"]: row["name"]
                for row in await db.execute_fetchall(
                    "SELECT a.alias, l.name FROM exercise_aliases a JOIN exercise_library l ON l.id = a.library_id"
                )
            }
            tex = {row["name"] for row in await db.execute_fetchall("SELECT name FROM training_exercises")}
            return _history(history), library, aliases, tex
        finally:
            await db.close()

    history, library, aliases, tex = asyncio.run(scenario())
    assert history == [("KB Halo", 8.0), ("KB Swing", 16.0), ("KB Swing", 20.0), ("Overhead Press", 40.0)]
    assert not {"Kettlebell Swing", "Shoulder Press"} & library
    assert not {"Kettlebell Swing", "Shoulder Press", "Halo (KB)"} & tex
    assert aliases["Kettlebell Swing"] == "KB Swing"
    assert aliases["Shoulder Press"] == "Overhead Press"
    assert aliases["Halo (KB)"] == "KB Halo"


def test_vocabulary_gains_movements_families_and_aliases(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.033_movement_taxonomy")
        db = await _db_at_032(tmp_path / "vocab.db")
        try:
            await migration.up(db)
            await db.commit()
            rows = await db.execute_fetchall(
                "SELECT l.name, f.name AS family, l.default_equipment, l.metric FROM exercise_library l "
                "LEFT JOIN exercise_families f ON f.id = l.family_id"
            )
            aliases = await db.execute_fetchall(
                "SELECT a.alias, l.name FROM exercise_aliases a JOIN exercise_library l ON l.id = a.library_id"
            )
            names = {row["name"].lower() for row in rows}
            alias_names = {row["alias"].lower() for row in aliases}
            return (
                {row["name"]: dict(row) for row in rows},
                {r["alias"]: r["name"] for r in aliases},
                names,
                alias_names,
            )
        finally:
            await db.close()

    library, aliases, names, alias_names = asyncio.run(scenario())
    assert library["Pistol Squat"]["family"] == "Squat"
    assert library["Hang Power Snatch"]["family"] == "Snatch"
    assert library["Wall Ball"]["default_equipment"] == "ball"
    assert library["Hanging Knee Raise"]["metric"] == "reps"
    assert aliases["pistols"] == "Pistol Squat"
    assert aliases["T2B"] == "Toes-to-bar"
    assert aliases["ball over shoulder"] == "D-ball Over Shoulder"
    assert not names & alias_names, "an alias equal to a library name would make matching ambiguous"


def test_equipment_outside_the_list_is_refused_by_the_schema(tmp_path):
    async def scenario():
        migration = importlib.import_module("app.migrations.033_movement_taxonomy")
        db = await _db_at_032(tmp_path / "check.db")
        try:
            await migration.up(db)
            session_id = await _session_with(db, "Back Squat", 60)
            try:
                await db.execute("UPDATE training_entries SET equipment = 'spoon' WHERE session_id = ?", (session_id,))
            except aiosqlite.IntegrityError:
                return True
            return False
        finally:
            await db.close()

    assert asyncio.run(scenario())
