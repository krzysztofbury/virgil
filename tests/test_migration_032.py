import asyncio
import importlib

import pytest


def test_training_history_indexes_are_created_idempotently(tmp_path):
    async def scenario():
        import aiosqlite

        migration = importlib.import_module("app.migrations.032_training_history_indexes")
        db = await aiosqlite.connect(tmp_path / "training-indexes.db")
        await db.execute("CREATE TABLE training_sessions (id INTEGER PRIMARY KEY, date TEXT NOT NULL)")
        await db.execute("CREATE TABLE training_entries (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL)")
        await migration.up(db)
        await migration.up(db)
        rows = await db.execute_fetchall("SELECT name FROM sqlite_master WHERE type = 'index' ORDER BY name")
        await db.close()
        return {row[0] for row in rows}

    indexes = asyncio.run(scenario())
    assert "idx_training_sessions_date_id" in indexes
    assert "idx_training_entries_session_id" in indexes


@pytest.mark.parametrize(
    "wrong_sql",
    [
        "CREATE INDEX idx_training_sessions_date_id ON training_sessions(id)",
        "CREATE UNIQUE INDEX idx_training_entries_session_id ON training_entries(session_id)",
        "CREATE INDEX idx_training_entries_session_id ON training_entries(session_id) WHERE session_id > 0",
        "CREATE INDEX idx_training_entries_session_id ON other_entries(session_id)",
    ],
)
def test_training_history_migration_rejects_wrong_existing_index(tmp_path, wrong_sql):
    async def scenario():
        import aiosqlite

        migration = importlib.import_module("app.migrations.032_training_history_indexes")
        db = await aiosqlite.connect(tmp_path / "wrong-training-index.db")
        await db.execute("CREATE TABLE training_sessions (id INTEGER PRIMARY KEY, date TEXT NOT NULL)")
        await db.execute("CREATE TABLE training_entries (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL)")
        await db.execute("CREATE TABLE other_entries (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL)")
        await db.execute(wrong_sql)
        await db.commit()
        try:
            await migration.up(db)
        finally:
            await db.close()

    with pytest.raises(RuntimeError, match="has incompatible definition"):
        asyncio.run(scenario())


def test_training_history_migration_retries_after_incompatible_index_is_removed(tmp_path):
    async def scenario():
        import aiosqlite

        migration = importlib.import_module("app.migrations.032_training_history_indexes")
        db = await aiosqlite.connect(tmp_path / "retry-training-index.db")
        await db.execute("CREATE TABLE training_sessions (id INTEGER PRIMARY KEY, date TEXT NOT NULL)")
        await db.execute("CREATE TABLE training_entries (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL)")
        await db.execute("CREATE UNIQUE INDEX idx_training_entries_session_id ON training_entries(session_id)")
        await db.commit()
        try:
            with pytest.raises(RuntimeError, match="idx_training_entries_session_id has incompatible definition"):
                await migration.up(db)
            await db.rollback()
            await db.execute("DROP INDEX idx_training_entries_session_id")
            await migration.up(db)
            rows = await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_training_%'"
            )
            return {row[0] for row in rows}
        finally:
            await db.close()

    assert asyncio.run(scenario()) == {
        "idx_training_sessions_date_id",
        "idx_training_entries_session_id",
    }
