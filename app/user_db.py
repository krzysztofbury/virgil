"""Per-user database management — create, connect, migrate."""

import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

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
    await db.execute("PRAGMA busy_timeout=5000")

    await run_migrations(db)
    await db.close()


class UserDatabaseMissing(FileNotFoundError):
    """The account's database file is gone: deleted, or replaced by a factory reset."""


async def open_user_db(db_filename: str) -> aiosqlite.Connection:
    """Open a connection to an existing per-user database.

    Read-write, never create: a request that loaded the account before a reset or
    delete would otherwise get a new, empty, unmigrated file nobody cleans up.
    """
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    db_path = Path(USERS_DB_DIR) / db_filename
    try:
        db = await aiosqlite.connect(f"file:{quote(str(db_path))}?mode=rw", uri=True)
    except sqlite3.OperationalError as error:
        if not db_path.exists():
            raise UserDatabaseMissing(db_filename) from error
        raise
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # Requests, the scheduler, and webhook syncs write concurrently — wait for
    # a held lock instead of failing instantly with "database is locked".
    await db.execute("PRAGMA busy_timeout=5000")
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

    Routers use this dependency to open the authenticated user's database.
    """
    db = getattr(request.state, "user_db", None)
    if db is None:
        raise RuntimeError("No user_db in request state — auth middleware did not run")
    return db
