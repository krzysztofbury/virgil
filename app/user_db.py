"""Per-user database management — create, connect, migrate."""

import os
from pathlib import Path

import aiosqlite
from starlette.requests import Request

from app.config import USERS_DB_DIR
from app.migrations.runner import run_migrations


async def create_user_db(db_filename: str) -> None:
    """Create a new per-user database and run all migrations."""
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    db_dir = Path(USERS_DB_DIR)
    db_dir.mkdir(parents=True, exist_ok=True)

    db_path = str(db_dir / db_filename)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row

    result = await db.execute_fetchall("PRAGMA journal_mode=WAL")
    assert result[0][0].lower() == "wal", f"WAL mode not enabled: {result}"
    await db.execute("PRAGMA foreign_keys=ON")

    await run_migrations(db)
    await db.close()


async def open_user_db(db_filename: str) -> aiosqlite.Connection:
    """Open a connection to an existing per-user database."""
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    db_path = str(Path(USERS_DB_DIR) / db_filename)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def close_user_db(db: aiosqlite.Connection) -> None:
    """Close a per-user database connection."""
    if db:
        await db.close()


def delete_user_db(db_filename: str) -> None:
    """Delete a per-user database file and its WAL/SHM files."""
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    db_path = Path(USERS_DB_DIR) / db_filename
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        if path.exists():
            os.remove(path)


def get_user_db_from_request(request: Request) -> aiosqlite.Connection:
    """Extract the per-user DB connection from request state.

    All routers should use this instead of get_db().
    """
    db = getattr(request.state, "user_db", None)
    if db is None:
        raise RuntimeError("No user_db in request state — auth middleware did not run")
    return db
