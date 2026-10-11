"""The shared service serializes collision checks and rolls back partial writes."""

import asyncio
import sqlite3

import aiosqlite
import pytest

from app.library_validation import LibraryWriteError
from app.migrations.runner import run_migrations
from app.services.library import write_library


def test_concurrent_unicode_names_have_one_winner(tmp_path):
    async def scenario():
        async with aiosqlite.connect(tmp_path / "library.db") as setup:
            setup.row_factory = aiosqlite.Row
            await run_migrations(setup)

        async def create(name):
            async with aiosqlite.connect(tmp_path / "library.db") as db:
                db.row_factory = aiosqlite.Row
                try:
                    await write_library(db, op="create", fields={"name": name})
                    return "created"
                except LibraryWriteError as exc:
                    assert exc.status == 409
                    return "conflict"

        return await asyncio.gather(create("Ćwiczenie"), create("ćwiczenie"))

    assert sorted(asyncio.run(scenario())) == ["conflict", "created"]
    with sqlite3.connect(tmp_path / "library.db") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM exercise_library WHERE name IN ('Ćwiczenie', 'ćwiczenie')"
        ).fetchone() == (1,)


def test_storage_failure_restores_fields_and_tags(tmp_path):
    async def scenario():
        async with aiosqlite.connect(tmp_path / "library.db") as db:
            db.row_factory = aiosqlite.Row
            await run_migrations(db)
            result = await write_library(
                db, op="create", fields={"name": "Atomic exercise", "notes": "original"}, tags=["old"]
            )
            await db.execute(
                "CREATE TRIGGER refuse_tag BEFORE INSERT ON exercise_library_tags "
                "WHEN NEW.tag = 'refused' BEGIN SELECT RAISE(ABORT, 'tag storage failure'); END"
            )
            await db.commit()
            with pytest.raises(sqlite3.IntegrityError, match="tag storage failure"):
                await write_library(
                    db, op="update", entry_id=result["id"], fields={"notes": "changed"}, tags=["refused"]
                )
            assert not db.in_transaction
            assert (await db.execute_fetchall("SELECT notes FROM exercise_library WHERE id = ?", (result["id"],)))[0][
                0
            ] == "original"
            assert [
                row[0]
                for row in await db.execute_fetchall(
                    "SELECT tag FROM exercise_library_tags WHERE library_id = ?", (result["id"],)
                )
            ] == ["old"]

    asyncio.run(scenario())
