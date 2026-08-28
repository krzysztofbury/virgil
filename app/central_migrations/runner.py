"""Discover and atomically apply central registry migrations."""

import importlib
import logging
import re
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent
TRACKING_TABLE = "central_schema_migrations"
_TRACKING_COLUMNS = [
    ("version", "INTEGER", 0, None, 1),
    ("name", "TEXT", 1, None, 0),
    ("applied_at", "TEXT", 1, "datetime('now')", 0),
]


async def _tracking_table_exists(db: aiosqlite.Connection) -> bool:
    rows = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (TRACKING_TABLE,),
    )
    return bool(rows)


async def _current_version(db: aiosqlite.Connection) -> int:
    """Return the current version without modifying an unversioned database."""
    applied = await _applied_migrations(db)
    return applied[-1][0] if applied else 0


async def has_application_schema(db: aiosqlite.Connection) -> bool:
    """Whether the database contains central application tables worth snapshotting."""
    rows = await db.execute_fetchall(
        """SELECT 1 FROM sqlite_master
           WHERE type = 'table'
             AND name NOT LIKE 'sqlite_%'
             AND name != ?
           LIMIT 1""",
        (TRACKING_TABLE,),
    )
    return bool(rows)


def _discover_migrations() -> list[tuple[int, str, str]]:
    pattern = re.compile(r"^([0-9]{3})_.+\.py$")
    discovered: list[tuple[int, str, str]] = []
    versions: set[int] = set()
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        match = pattern.match(path.name)
        if not match:
            if path.is_file() and path.name[:1].isdigit():
                raise RuntimeError(f"Unsupported central migration filename: {path.name}")
            continue
        version = int(match.group(1))
        if version in versions:
            raise RuntimeError(f"Duplicate central migration version: {version:03d}")
        versions.add(version)
        discovered.append((version, path.stem, path.name))
    expected_versions = list(range(1, len(discovered) + 1))
    actual_versions = [version for version, _module, _filename in discovered]
    if actual_versions != expected_versions:
        raise RuntimeError(f"Central migration versions must be contiguous from 001: {actual_versions}")
    return discovered


async def _applied_migrations(db: aiosqlite.Connection) -> list[tuple[int, str]]:
    if not await _tracking_table_exists(db):
        return []
    columns = await db.execute_fetchall(f"PRAGMA table_info({TRACKING_TABLE})")
    actual_columns = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual_columns != _TRACKING_COLUMNS:
        raise RuntimeError("Central migration tracking table has an unsupported schema")
    rows = await db.execute_fetchall(f"SELECT version, name FROM {TRACKING_TABLE} ORDER BY version")  # noqa: S608
    return [(row["version"], row["name"]) for row in rows]


async def _pending_migrations(db: aiosqlite.Connection) -> list[tuple[int, str, str]]:
    discovered = _discover_migrations()
    applied = await _applied_migrations(db)
    latest_version = discovered[-1][0] if discovered else 0
    if applied and applied[-1][0] > latest_version:
        raise RuntimeError(
            f"Central database version {applied[-1][0]:03d} is newer than supported version {latest_version:03d}"
        )
    expected_prefix = [(version, filename) for version, _module, filename in discovered[: len(applied)]]
    if applied != expected_prefix:
        raise RuntimeError("Central migration history does not match this application")
    return discovered[len(applied) :]


async def count_pending_migrations(db: aiosqlite.Connection) -> int:
    return len(await _pending_migrations(db))


async def _validate_migration(db: aiosqlite.Connection, module_name: str) -> None:
    migration = importlib.import_module(f"app.central_migrations.{module_name}")
    validator = getattr(migration, "validate", None)
    if validator is None:
        raise RuntimeError(f"Central migration {module_name} has no schema validator")
    await validator(db)


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Apply each central migration and its version marker in one transaction."""
    if db.in_transaction:
        raise RuntimeError("Central migrations require a connection with no active transaction")

    pending = await _pending_migrations(db)
    for version, module_name, filename in pending:
        logger.info("Applying central migration %03d: %s", version, filename)
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute(
                f"""CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                )"""  # noqa: S608
            )
            migration = importlib.import_module(f"app.central_migrations.{module_name}")
            await migration.up(db)
            await _validate_migration(db, module_name)
            await db.execute(
                f"INSERT INTO {TRACKING_TABLE} (version, name) VALUES (?, ?)",  # noqa: S608
                (version, filename),
            )
            await db.commit()
        except BaseException as error:
            try:
                await db.rollback()
            except Exception:
                logger.exception("Rollback also failed for central migration %03d", version)
            if isinstance(error, Exception):
                logger.exception("Central migration %03d failed: %s", version, filename)
            raise

    applied = await _applied_migrations(db)
    if not applied:
        raise RuntimeError("Central migration tracking table is missing")
    current_module = _discover_migrations()[len(applied) - 1][1]
    await _validate_migration(db, current_module)
