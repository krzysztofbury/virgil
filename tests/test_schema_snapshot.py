"""The schema at head matches a checked-in snapshot.

A new migration, an edit to app/db.py SCHEMA or to a shipped migration all
change what a fresh database looks like. The snapshot makes that a deliberate
diff in review. To accept a change, regenerate the files:

    VIRGIL_UPDATE_SCHEMA_SNAPSHOT=1 uv run --locked --extra dev pytest -q tests/test_schema_snapshot.py

The stored CREATE text of a renamed table is rewritten by SQLite itself, so the
snapshot belongs to the uv-managed Python (and its SQLite) that CI uses too.
"""

import asyncio
import os
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

SNAPSHOTS = Path(__file__).parent / "snapshots"


async def _user_database(path: Path) -> aiosqlite.Connection:
    import app.user_db as user_db

    await user_db.create_user_db(path.name)
    return await user_db.open_user_db(path.name)


async def _central_database(path: Path) -> aiosqlite.Connection:
    from app.central_migrations.runner import run_migrations

    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await run_migrations(db)
    return db


async def _dump(db: aiosqlite.Connection) -> str:
    rows = await db.execute_fetchall(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type, name"
    )
    return "\n".join(f"-- {row['type']} {row['name']}\n{row['sql']};\n" for row in rows)


@pytest.mark.parametrize(
    ("snapshot", "build"), [("user_schema.sql", _user_database), ("central_schema.sql", _central_database)]
)
def test_the_schema_at_head_matches_the_snapshot(tmp_path, monkeypatch, snapshot, build):
    import app.user_db as user_db

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))

    async def scenario():
        db = await build(tmp_path / "head.db")
        try:
            return await _dump(db)
        finally:
            await db.close()

    actual = asyncio.run(scenario())
    path = SNAPSHOTS / snapshot
    if os.environ.get("VIRGIL_UPDATE_SCHEMA_SNAPSHOT") == "1":
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual)
    assert actual == path.read_text(), (
        f"schema changed (SQLite {sqlite3.sqlite_version}): regenerate {path.name} if that was intended"
    )
