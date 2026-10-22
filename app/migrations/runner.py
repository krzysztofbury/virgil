"""Migration runner — discovers and applies numbered migration scripts."""

import importlib
import logging
import re
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent


async def _current_version(db: aiosqlite.Connection) -> int:
    """Highest applied migration version (ensures the tracking table exists)."""
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    await db.commit()
    row = await db.execute_fetchall("SELECT MAX(version) as v FROM schema_migrations")
    return row[0]["v"] if row and row[0]["v"] is not None else 0


class DatabaseNewerError(RuntimeError):
    """The database was migrated by newer code, so this code does not know its schema."""


def _discover() -> list[tuple[int, str, str]]:
    """(version, module_name, filename) for every migration shipped with this code."""
    pattern = re.compile(r"^(\d{3})_.+\.py$")
    found = []
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        m = pattern.match(f.name)
        if m:
            found.append((int(m.group(1)), f.stem, f.name))
    return sorted(found)


def latest_version() -> int:
    """The newest migration this code ships."""
    migrations = _discover()
    return migrations[-1][0] if migrations else 0


def _discover_pending(current: int) -> list[tuple[int, str, str]]:
    """(version, module_name, filename) for every migration above `current`.

    Refuses a database at a version this code has never heard of: after an image
    rollback, old code on a newer schema would otherwise run on unchecked.
    """
    migrations = _discover()
    latest = migrations[-1][0] if migrations else 0
    if current > latest:
        raise DatabaseNewerError(f"Database is at migration {current:03d}, newer than this code's {latest:03d}")
    return [migration for migration in migrations if migration[0] > current]


async def count_pending_migrations(db: aiosqlite.Connection) -> int:
    """How many migrations run_migrations() would apply — used to decide
    whether a pre-migration snapshot is warranted (migrations are one-way;
    an image rollback cannot undo them)."""
    return len(_discover_pending(await _current_version(db)))


async def run_migrations(db: aiosqlite.Connection, *, up_to: int | None = None) -> None:
    """Run all pending migrations in order, each in its own immediate transaction.

    `up_to` stops after that version: a data import rebuilds the schema its
    export was made with, loads the rows, then runs the rest.

    Without an explicit BEGIN, Python's sqlite3 opens a transaction only at the
    first INSERT/UPDATE/DELETE, so DDL before it committed on its own and a
    failure could leave a half-applied migration behind.
    """
    if db.in_transaction:
        raise RuntimeError("Migrations require a connection with no active transaction")
    migrations = _discover_pending(await _current_version(db))
    if up_to is not None:
        migrations = [migration for migration in migrations if migration[0] <= up_to]

    if not migrations:
        logger.debug("No pending migrations")
        return

    for version, module_name, filename in migrations:
        logger.info("Applying migration %03d: %s", version, filename)
        await db.execute("BEGIN IMMEDIATE")
        try:
            mod = importlib.import_module(f"app.migrations.{module_name}")
            await mod.up(db)
            if not db.in_transaction:
                # A commit (or executescript) inside up() split the migration.
                raise RuntimeError(f"Migration {version:03d} ended the runner's transaction itself")
            await db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, filename),
            )
            await db.commit()
            logger.info("Migration %03d applied successfully", version)
        except BaseException as error:
            try:
                await db.rollback()
            except Exception:
                logger.exception("Rollback also failed for migration %03d", version)
            if isinstance(error, Exception):
                logger.exception("Migration %03d FAILED: %s", version, filename)
            # Stop applying further migrations: the schema is in an unknown state.
            raise
