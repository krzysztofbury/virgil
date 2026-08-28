import asyncio
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
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


def _validate_backup(path: Path) -> None:
    """Reject truncated or corrupt snapshots before they become recovery artifacts."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"SQLite snapshot integrity check failed: {path.name}")
    finally:
        connection.close()


def _publish_backup(src_path: str, dst: Path, semantic_validator: Callable[[Path], None]) -> None:
    """Build privately, validate, then publish without replacing another writer's snapshot."""
    if dst.exists():
        _validate_backup(dst)
        semantic_validator(dst)
        return

    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        _do_backup(src_path, str(temporary))
        _validate_backup(temporary)
        semantic_validator(temporary)
        try:
            os.link(temporary, dst)
        except FileExistsError:
            _validate_backup(dst)
            semantic_validator(dst)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_snapshot_version(path: Path, tracking_table: str, expected_version: int, *, absent_at_zero: bool) -> None:
    if tracking_table not in {"schema_migrations", "central_schema_migrations"}:
        raise ValueError(f"Unsupported migration tracking table: {tracking_table}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (tracking_table,)
        ).fetchone()
        if table is None:
            if expected_version == 0:
                return
            raise RuntimeError(f"Snapshot {path.name} has no migration history")
        if expected_version == 0 and absent_at_zero:
            raise RuntimeError(f"Snapshot {path.name} was taken after migration tracking began")
        row = connection.execute(f"SELECT MAX(version) FROM {tracking_table}").fetchone()
        actual_version = row[0] if row and row[0] is not None else 0
        if actual_version != expected_version:
            raise RuntimeError(
                f"Snapshot {path.name} has schema version {actual_version:03d}, expected {expected_version:03d}"
            )
    finally:
        connection.close()


def _schema_fingerprint(path: Path) -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """SELECT type, name, tbl_name, sql FROM sqlite_master
               WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()
        return tuple((object_type, name, table, "".join(sql.lower().split())) for object_type, name, table, sql in rows)
    finally:
        connection.close()


def _validate_central_snapshot(
    path: Path,
    expected_version: int,
    expected_schema: tuple[tuple[str, str, str, str], ...],
) -> None:
    """Prove the artifact is the expected central registry, not just valid SQLite."""
    from app.central_migrations.runner import _discover_migrations

    _validate_snapshot_version(
        path,
        "central_schema_migrations",
        expected_version,
        absent_at_zero=True,
    )
    actual = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        if _schema_fingerprint(path) != expected_schema:
            raise RuntimeError(f"Snapshot {path.name} is not the expected central schema")

        if expected_version > 0:
            history = actual.execute("SELECT version, name FROM central_schema_migrations ORDER BY version").fetchall()
            expected_history = [
                (version, filename)
                for version, _module, filename in _discover_migrations()
                if version <= expected_version
            ]
            if history != expected_history:
                raise RuntimeError(f"Snapshot {path.name} has unexpected central migration history")
    finally:
        actual.close()


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
    validator = partial(
        _validate_snapshot_version,
        tracking_table="schema_migrations",
        expected_version=version,
        absent_at_zero=False,
    )
    await asyncio.to_thread(_publish_backup, src_path, dst, validator)
    await asyncio.to_thread(_prune_backups, stem, PRE_MIGRATION_MAX_COPIES, directory)
    logger.info("Pre-migration snapshot created: %s", dst.name)
    return dst


async def snapshot_central_before_migration(db, current_version: int) -> Path:
    """Keep the pristine central registry before its first pending migration."""
    src_path = await db_main_path(db)
    directory = _pre_migration_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem
    dst = directory / f"{stem}-pre-migration-v{current_version:03d}.db"
    expected_schema = await asyncio.to_thread(_schema_fingerprint, Path(src_path))
    validator = partial(
        _validate_central_snapshot,
        expected_version=current_version,
        expected_schema=expected_schema,
    )
    await asyncio.to_thread(_publish_backup, src_path, dst, validator)
    await asyncio.to_thread(_prune_backups, stem, PRE_MIGRATION_MAX_COPIES, directory)
    logger.info("Central pre-migration snapshot created: %s", dst.name)
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
