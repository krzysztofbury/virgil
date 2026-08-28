"""Migration 026: make training exercise names unique without losing history."""

import asyncio
import importlib

import aiosqlite
import pytest


async def _legacy_db(tmp_path):
    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
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
        """CREATE TABLE training_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER NOT NULL REFERENCES training_exercises(id)
        )"""
    )
    keeper = await db.execute(
        """INSERT INTO training_exercises
           (name, section, notes, display_order, archived, ad_hoc)
           VALUES ('Thruster', 'Core', '', 9, 1, 1)"""
    )
    duplicate = await db.execute(
        """INSERT INTO training_exercises
           (name, section, notes, display_order, archived, ad_hoc)
           VALUES ('thruster', 'Core', 'Added from a WOD', 8, 0, 0)"""
    )
    await db.execute("INSERT INTO training_entries (exercise_id) VALUES (?)", (keeper.lastrowid,))
    await db.execute("INSERT INTO training_entries (exercise_id) VALUES (?)", (duplicate.lastrowid,))
    await db.commit()
    return db, keeper.lastrowid, duplicate.lastrowid


def test_duplicate_names_merge_and_repoint_history(tmp_path):
    async def run():
        migration = importlib.import_module("app.migrations.026_training_exercise_name_unique")
        db, keeper_id, _duplicate_id = await _legacy_db(tmp_path)
        try:
            await migration.up(db)
            await migration.up(db)
            await db.commit()

            exercises = await db.execute_fetchall(
                "SELECT id, name, section, notes, display_order, archived, ad_hoc FROM training_exercises"
            )
            assert [dict(row) for row in exercises] == [
                {
                    "id": keeper_id,
                    "name": "Thruster",
                    "section": "Core",
                    "notes": "Added from a WOD",
                    "display_order": 8,
                    "archived": 0,
                    "ad_hoc": 0,
                }
            ]
            entries = await db.execute_fetchall("SELECT exercise_id FROM training_entries ORDER BY id")
            assert [row["exercise_id"] for row in entries] == [keeper_id, keeper_id]

            with pytest.raises(aiosqlite.IntegrityError):
                await db.execute("INSERT INTO training_exercises (name, section) VALUES ('THRUSTER', 'Core')")
        finally:
            await db.close()

    asyncio.run(run())


def test_semantically_different_duplicates_abort_without_data_loss(tmp_path):
    async def run():
        migration = importlib.import_module("app.migrations.026_training_exercise_name_unique")
        db, keeper_id, duplicate_id = await _legacy_db(tmp_path)
        try:
            await db.execute("UPDATE training_exercises SET section = 'Cardio' WHERE id = ?", (duplicate_id,))
            await db.commit()

            with pytest.raises(RuntimeError, match="different semantics"):
                await migration.up(db)
            await db.rollback()

            exercises = await db.execute_fetchall("SELECT id FROM training_exercises ORDER BY id")
            assert [row["id"] for row in exercises] == [keeper_id, duplicate_id]
            entries = await db.execute_fetchall("SELECT exercise_id FROM training_entries ORDER BY id")
            assert [row["exercise_id"] for row in entries] == [keeper_id, duplicate_id]
        finally:
            await db.close()

    asyncio.run(run())


def test_retry_after_mid_migration_failure_reopens_cleanly(tmp_path):
    async def run():
        migration = importlib.import_module("app.migrations.026_training_exercise_name_unique")
        db, keeper_id, _duplicate_id = await _legacy_db(tmp_path)
        db_path = tmp_path / "legacy.db"
        try:
            await db.execute(
                """CREATE TRIGGER fail_duplicate_delete
                   BEFORE DELETE ON training_exercises
                   BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
            )
            await db.commit()
            with pytest.raises(aiosqlite.IntegrityError, match="injected migration failure"):
                await migration.up(db)
            await db.rollback()
            await db.execute("DROP TRIGGER fail_duplicate_delete")
            await db.commit()
        finally:
            await db.close()

        reopened = await aiosqlite.connect(db_path)
        reopened.row_factory = aiosqlite.Row
        await reopened.execute("PRAGMA foreign_keys=ON")
        try:
            await migration.up(reopened)
            await reopened.commit()
            exercises = await reopened.execute_fetchall("SELECT id FROM training_exercises")
            assert [row["id"] for row in exercises] == [keeper_id]
            entries = await reopened.execute_fetchall("SELECT exercise_id FROM training_entries ORDER BY id")
            assert [row["exercise_id"] for row in entries] == [keeper_id, keeper_id]
        finally:
            await reopened.close()

    asyncio.run(run())
