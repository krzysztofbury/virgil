import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from app.config import CENTRAL_DB_PATH
from app.db import get_setting

logger = logging.getLogger(__name__)

# Anchored to the central DB dir (the mounted /data volume in Docker) so
# backups survive container rebuilds. The legacy DB_PATH pointed inside
# the image in prod, silently backing up a nonexistent (empty) database.
BACKUP_DIR = Path(CENTRAL_DB_PATH).parent / "backups"


def _pre_migration_dir() -> Path:
    """Pre-migration snapshots live in their own subdirectory — the rotating
    prune globs `{stem}-*.db` and would otherwise match snapshot names, keep
    them forever ('p' sorts after digits) and evict every regular backup.
    Resolved at call time so tests can monkeypatch BACKUP_DIR."""
    return BACKUP_DIR / "pre-migration"


def _do_backup(src_path: str, dst_path: str) -> None:
    """Blocking SQLite backup via sqlite3.Connection.backup()."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _prune_backups(stem: str, max_copies: int, directory: Path | None = None) -> None:
    """Remove oldest backups for one database beyond max_copies."""
    directory = directory if directory is not None else BACKUP_DIR
    if not directory.exists():
        return
    backups = sorted(directory.glob(f"{stem}-*.db"), key=lambda p: p.name)
    while len(backups) > max_copies:
        oldest = backups.pop(0)
        # The scheduler and a manual "Backup Now" can prune the same stem in
        # parallel threads — losing the unlink race must not fail the backup.
        oldest.unlink(missing_ok=True)
        logger.info("Pruned old backup: %s", oldest.name)


async def db_main_path(db) -> str:
    """Resolve the on-disk path of the connection's main database."""
    rows = await db.execute_fetchall("PRAGMA database_list")
    for row in rows:
        if row["name"] == "main" and row["file"]:
            return row["file"]
    raise RuntimeError("Cannot back up: main database has no file path (in-memory?)")


def _timestamp() -> str:
    """Filename timestamp in UTC — local time breaks the 'lexicographic sort ==
    chronological' invariant the prune relies on for one hour every DST fold.
    Minute precision: repeated backups within a schedule interval overwrite
    instead of piling up."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M")


async def run_backup(db) -> Path:
    """Create a consistent SQLite backup of THIS connection's database and prune old copies.

    The source path is derived from the connection itself (PRAGMA database_list),
    so per-user databases are backed up correctly — never the legacy global DB_PATH.
    Timestamped names: an hourly schedule keeps distinct copies instead of
    overwriting one date-named file all day.
    """
    src_path = await db_main_path(db)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem
    dst = BACKUP_DIR / f"{stem}-{_timestamp()}.db"

    await asyncio.to_thread(_do_backup, src_path, str(dst))

    max_copies = int(await get_setting(db, "backup_max_copies", "7"))
    await asyncio.to_thread(_prune_backups, stem, max_copies)

    logger.info("Backup created: %s", dst.name)
    return dst


PRE_MIGRATION_MAX_COPIES = 3


async def snapshot_before_migration(db) -> Path:
    """Snapshot taken right before pending migrations run.

    Migrations are one-way — rolling back to an older image cannot restore the
    schema, so this snapshot is the only path back after a bad migration.
    Keyed by the CURRENT schema version and never overwritten: a failed
    migration followed by a restart would otherwise replace the pristine
    snapshot with a copy of the half-migrated database.
    """
    from app.migrations.runner import _current_version

    src_path = await db_main_path(db)
    directory = _pre_migration_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem

    version = await _current_version(db)
    dst = directory / f"{stem}-pre-migration-v{version:03d}.db"
    if dst.exists():
        # A snapshot of this exact schema state already exists — it is the
        # pristine copy; the current database may already be half-migrated.
        return dst

    await asyncio.to_thread(_do_backup, src_path, str(dst))
    await asyncio.to_thread(_prune_backups, stem, PRE_MIGRATION_MAX_COPIES, directory)
    logger.info("Pre-migration snapshot created: %s", dst.name)
    return dst


CENTRAL_BACKUP_MAX_AGE_HOURS = 24
CENTRAL_BACKUP_MAX_COPIES = 7


async def maybe_backup_central() -> Path | None:
    """Back up the central registry (identities, MFA, webhook routes) at most
    once per CENTRAL_BACKUP_MAX_AGE_HOURS.

    Per-user scheduled backups never cover this database, yet losing it orphans
    every per-user DB (filenames/credentials live here). The age guard is
    file-mtime based so it survives restarts.
    """
    src = Path(CENTRAL_DB_PATH)
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = src.stem

    existing = sorted(BACKUP_DIR.glob(f"{stem}-*.db"), key=lambda p: p.stat().st_mtime)
    if existing and (time.time() - existing[-1].stat().st_mtime) < CENTRAL_BACKUP_MAX_AGE_HOURS * 3600:
        return None

    dst = BACKUP_DIR / f"{stem}-{_timestamp()}.db"
    await asyncio.to_thread(_do_backup, str(src), str(dst))
    await asyncio.to_thread(_prune_backups, stem, CENTRAL_BACKUP_MAX_COPIES)
    logger.info("Central DB backup created: %s", dst.name)
    return dst
