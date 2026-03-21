"""Migration runner — discovers and applies numbered migration scripts."""

import importlib
import logging
import re
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent


async def run_migrations(db: aiosqlite.Connection) -> None:
    """Run all pending migrations in order."""
    # Ensure schema_migrations table exists
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    await db.commit()

    # Get current version
    row = await db.execute_fetchall("SELECT MAX(version) as v FROM schema_migrations")
    current = row[0]["v"] if row and row[0]["v"] is not None else 0

    # Discover migration files (NNN_name.py)
    pattern = re.compile(r"^(\d{3})_.+\.py$")
    migrations = []
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        m = pattern.match(f.name)
        if m:
            version = int(m.group(1))
            if version > current:
                migrations.append((version, f.stem, f.name))

    if not migrations:
        logger.debug("No pending migrations")
        return

    for version, module_name, filename in sorted(migrations):
        logger.info("Applying migration %03d: %s", version, filename)
        try:
            mod = importlib.import_module(f"app.migrations.{module_name}")
            await mod.up(db)
            await db.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (version, filename),
            )
            await db.commit()
            logger.info("Migration %03d applied successfully", version)
        except Exception:
            logger.exception("Migration %03d FAILED: %s", version, filename)
            # Attempt rollback of uncommitted changes.
            try:
                await db.rollback()
            except Exception:
                logger.exception("Rollback also failed for migration %03d", version)
            # Stop applying further migrations — the schema is in an unknown state.
            raise
