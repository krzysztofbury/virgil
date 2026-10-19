import asyncio
import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
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
    with (
        closing(sqlite3.connect(Path(src_path).resolve().as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(dst_path)) as dst,
    ):
        src.backup(dst)
        # Recovery artifacts are standalone files, with no WAL sidecars.
        dst.execute("PRAGMA journal_mode=DELETE")


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


def _prune_backups(
    stem: str,
    max_copies: int,
    directory: Path | None = None,
    protected: Path | None = None,
) -> None:
    """Remove oldest backups for one database beyond max_copies."""
    directory = directory if directory is not None else BACKUP_DIR
    if not directory.exists():
        return
    backups = sorted(directory.glob(f"{stem}-*.db"), key=lambda p: p.name)
    while len(backups) > max_copies:
        oldest = next((path for path in backups if path != protected), None)
        if oldest is None:
            break
        backups.remove(oldest)
        # The scheduler and a manual "Backup Now" can prune the same stem in
        # parallel threads — losing the unlink race must not fail the backup.
        oldest.unlink(missing_ok=True)
        logger.info("Pruned old backup: %s", oldest.name)


def delete_user_backups(db_filename: str) -> list[str]:
    """Remove every backup and pre-migration snapshot of one per-user database.

    Deleting an account or resetting it promises its data is gone, and a reset
    moves the account to a new file name, so nothing would ever prune these.
    """
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    stem = Path(db_filename).stem
    removed = []
    for directory, pattern in ((BACKUP_DIR, f"{stem}-*.db"), (_pre_migration_dir(), f"{stem}-pre-migration-v*.db")):
        if not directory.exists():
            continue
        for path in directory.glob(pattern):
            path.unlink(missing_ok=True)
            removed.append(path.name)
    if removed:
        logger.info("Deleted %d backup(s) of %s", len(removed), db_filename)
    return removed


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


async def run_backup(db, *, artifact_timestamp: str | None = None) -> Path:
    """Create a consistent SQLite backup of THIS connection's database and prune old copies.

    The source path is derived from the connection itself (PRAGMA database_list),
    so per-user databases are backed up correctly — never the legacy global DB_PATH.
    Timestamped names: an hourly schedule keeps distinct copies instead of
    overwriting one date-named file all day.
    """
    src_path = await db_main_path(db)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem
    timestamp = artifact_timestamp or _timestamp()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{4}(?:-j\d{20})?", timestamp):
        raise ValueError("Backup artifact timestamp must use YYYY-MM-DDTHHMM with an optional fixed-width job suffix")
    dst = BACKUP_DIR / f"{stem}-{timestamp}.db"

    await asyncio.to_thread(_publish_backup, src_path, dst, lambda _path: None)

    max_copies = int(await get_setting(db, "backup_max_copies", "7"))
    await asyncio.to_thread(_prune_backups, stem, max_copies, None, dst)

    logger.info("Backup created: %s", dst.name)
    return dst


# One missed tick is normal (a restart, a slow queue); two in a row are not.
BACKUP_OVERDUE_INTERVALS = 2

_ARTIFACT_TIME_RE = r"(\d{4}-\d{2}-\d{2}T\d{4})(?:-j\d{20})?\.db"


def _latest_backup_time(stem: str) -> datetime | None:
    """When the newest regular backup of one database was taken, from its name."""
    if not BACKUP_DIR.exists():
        return None
    name = re.compile(re.escape(stem) + "-" + _ARTIFACT_TIME_RE)
    taken = [
        datetime.strptime(match[1], "%Y-%m-%dT%H%M").replace(tzinfo=UTC)
        for path in BACKUP_DIR.glob(f"{stem}-*.db")
        if (match := name.fullmatch(path.name))
    ]
    return max(taken, default=None)


def _ago(then: datetime, now: datetime) -> str:
    minutes = max(0, int((now - then).total_seconds() // 60))
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size:
            count = minutes // size
            return f"{count} {unit}{'' if count == 1 else 's'} ago"
    return "just now"


async def backup_status(db, now: datetime | None = None) -> dict:
    """What Settings tells the user about this account's backups.

    The newest file on disk says how old the latest copy is; the newest backup
    job says whether the attempt after it is still running or failed. Either
    alone misleads: the file cannot show a failure, and the job history is pruned.
    """
    now = now or datetime.now(UTC)
    stem = Path(await db_main_path(db)).stem
    latest = await asyncio.to_thread(_latest_backup_time, stem)
    enabled = await get_setting(db, "backup_enabled", "1") == "1"
    try:
        interval_hours = float(await get_setting(db, "backup_interval_hours", "24"))
    except ValueError:
        interval_hours = 24.0
    jobs = await db.execute_fetchall(
        "SELECT status, COALESCE(finished_at, created_at) AS at FROM jobs "
        "WHERE kind = 'backup' ORDER BY created_at DESC, id DESC LIMIT 1"
    )
    job = jobs[0] if jobs else None

    status = {
        "enabled": enabled,
        "latest_age": _ago(latest, now) if latest else None,
        "latest_local": latest.astimezone().strftime("%Y-%m-%d %H:%M") if latest else None,
        "failed_age": None,
    }
    if job and job["status"] in {"queued", "running"}:
        status["state"] = "running"
    elif job and job["status"] in {"failed", "needs_attention"}:
        status["state"] = "failed"
        status["failed_age"] = _ago(datetime.fromisoformat(job["at"]).replace(tzinfo=UTC), now)
    elif not enabled:
        status["state"] = "off"
    elif latest is None:
        status["state"] = "none"
    elif now - latest > timedelta(hours=BACKUP_OVERDUE_INTERVALS * interval_hours):
        status["state"] = "overdue"
    else:
        status["state"] = "ok"
    return status


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

    existing = sorted(BACKUP_DIR.glob(f"{stem}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in existing:
        if time.time() - path.stat().st_mtime >= CENTRAL_BACKUP_MAX_AGE_HOURS * 3600:
            break
        try:
            await asyncio.to_thread(_validate_backup, path)
        except (sqlite3.DatabaseError, RuntimeError):
            logger.warning("Ignoring invalid central backup: %s", path.name)
            continue
        return None

    # A unique suffix also permits recovery from a corrupt file in this minute.
    dst = BACKUP_DIR / f"{stem}-{_timestamp()}-{uuid.uuid4().hex}.db"
    await asyncio.to_thread(_publish_backup, str(src), dst, lambda _path: None)
    await asyncio.to_thread(_prune_backups, stem, CENTRAL_BACKUP_MAX_COPIES, None, dst)
    logger.info("Central DB backup created: %s", dst.name)
    return dst
