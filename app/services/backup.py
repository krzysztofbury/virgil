import asyncio
import filecmp
import logging
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from functools import partial
from math import isfinite
from pathlib import Path

from app.config import BACKUP_DIR_SETTING, CENTRAL_DB_PATH
from app.db import get_setting
from app.formatting import time_ago

logger = logging.getLogger(__name__)

# Anchored to the central DB dir (the mounted /data volume in Docker) so
# backups survive container rebuilds. The legacy DB_PATH pointed inside
# the image in prod, silently backing up a nonexistent (empty) database.
DATA_VOLUME_BACKUP_DIR = Path(CENTRAL_DB_PATH).parent / "backups"
# VIRGIL_BACKUP_DIR moves them to a mount of their own.
BACKUP_DIR = Path(BACKUP_DIR_SETTING) if BACKUP_DIR_SETTING else DATA_VOLUME_BACKUP_DIR


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
    """Remove every backup and pre-migration snapshot of one per-user database,
    and queue the removal of its off-site copies.

    Deleting an account or resetting it promises its data is gone, and a reset
    moves the account to a new file name, so nothing would ever prune these.
    """
    if not db_filename or ".." in db_filename or "/" in db_filename:
        raise ValueError(f"Invalid db_filename: {db_filename}")
    stem = Path(db_filename).stem
    removed = []
    # The data volume too: a copy that could not be moved off it at startup is
    # still this account's data.
    roots = dict.fromkeys((BACKUP_DIR, DATA_VOLUME_BACKUP_DIR))
    for root in roots:
        for directory, pattern in (
            (root, f"{stem}-*.db"),
            (root / "pre-migration", f"{stem}-pre-migration-v*.db"),
            # A copy cut short by a crash leaves its private name behind, and
            # it holds this account's data as much as a finished one does.
            (root, f".{stem}-*.tmp"),
            (root / "pre-migration", f".{stem}-pre-migration-v*.tmp"),
            (root / "pre-import", f"{stem}-pre-import-*.db"),
            (root / "pre-import", f".{stem}-pre-import-*.tmp"),
        ):
            if not directory.exists():
                continue
            for path in directory.glob(pattern):
                path.unlink(missing_ok=True)
                removed.append(path.name)
    if removed:
        logger.info("Deleted %d backup(s) of %s", len(removed), db_filename)
    from app.services.data_transfer import discard_account_imports
    from app.services.offsite import queue_deletion

    discard_account_imports(stem)
    queue_deletion(db_filename)
    return removed


def backup_dir_problem() -> str | None:
    """Why no backup can be written, or None.

    Checked by doing what a backup does: write a file and hard-link it into
    place. A host directory Docker created for root fails the write; an exFAT or
    FAT disk fails the link. Either way every backup and pre-migration snapshot
    would fail, and with them every pending migration.
    """
    probe = BACKUP_DIR / f".write-check.{uuid.uuid4().hex}.tmp"
    linked = probe.with_name(probe.name + ".link")
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        probe.write_bytes(b"")
        os.link(probe, linked)
    except OSError as exc:
        return (
            f"Backup directory {BACKUP_DIR} cannot hold backups as uid {os.getuid()} ({exc.strerror}): "
            "no backup or pre-migration snapshot can be made, and pending migrations will not run. "
            "It needs a Linux filesystem the container user can write."
        )
    finally:
        probe.unlink(missing_ok=True)
        linked.unlink(missing_ok=True)
    return None


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_copy(source: Path, destination: Path) -> None:
    """Copy with its timestamps, on disk before the final name appears.

    The modification time is the backup's age in Settings. The flushes matter
    because the source is deleted next, and a NAS restarting after a power cut
    is exactly when this runs.
    """
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        with open(temporary, "rb") as handle:
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        _fsync_dir(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def relocate_data_volume_backups() -> int:
    """Move backups left on the data volume into BACKUP_DIR once it points elsewhere.

    Left behind they would never be pruned, and Settings would not count them.
    Runs at startup, before anything writes a backup. A source is deleted only
    once an identical copy is on disk under the new directory.
    """
    source, target = DATA_VOLUME_BACKUP_DIR, BACKUP_DIR
    if not source.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    # Two paths can name one directory, as when one host folder is mounted at
    # both /data/backups and /backups. Then "moving" a file deletes the only
    # copy, so compare the directories themselves, not their spelling.
    if os.path.samefile(source, target):
        return 0
    if target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(target.resolve()):
        raise RuntimeError(f"VIRGIL_BACKUP_DIR {target} must not overlap {source}")
    moved = 0
    for path in sorted(source.rglob("*.db")):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            _durable_copy(path, destination)
        elif os.path.samefile(path, destination):
            # One file under two names (a nested mount): deleting either deletes both.
            continue
        if not filecmp.cmp(path, destination, shallow=False):
            # Not a copy of this file, for example one cut short by a power loss
            # after the link. Both stay for the operator to compare.
            logger.warning("Kept %s on the data volume: %s holds a different file", path, destination)
            continue
        _fsync_dir(destination.parent)
        path.unlink()
        moved += 1
    return moved


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


def _latest_backup(stem: str) -> tuple[datetime, str] | None:
    """When the newest regular backup of one database was written, and its name.

    The write time, not the time in the name: a job's file is named after the
    job's creation, which can be days before a delayed run actually wrote it.
    """
    if not BACKUP_DIR.exists():
        return None
    newest = max(
        ((path.stat().st_mtime, path.name) for path in BACKUP_DIR.glob(f"{stem}-*.db")),
        default=None,
    )
    if newest is None:
        return None
    return datetime.fromtimestamp(newest[0], UTC), newest[1]


async def backup_status(db, now: datetime | None = None) -> dict:
    """What Settings tells the user about this account's backups.

    The newest file on disk says how old the latest copy is; the backup jobs say
    whether one is on its way or the last attempt failed. Either alone misleads:
    a file cannot show a failure, and the job history is pruned.
    """
    now = now or datetime.now(UTC)
    stem = Path(await db_main_path(db)).stem
    latest = await asyncio.to_thread(_latest_backup, stem)
    enabled = await get_setting(db, "backup_enabled", "1") == "1"
    try:
        interval_hours = float(await get_setting(db, "backup_interval_hours", "24"))
    except ValueError:
        interval_hours = 24.0
    if not (isfinite(interval_hours) and 1 <= interval_hours <= 168):
        # The bounds the Automation form saves; anything else came from elsewhere.
        interval_hours = 24.0
    active = await db.execute_fetchall(
        "SELECT 1 FROM jobs WHERE kind = 'backup' AND status IN ('queued', 'running') LIMIT 1"
    )
    # By finish, not creation: a retried older job can finish after a newer one.
    finished = await db.execute_fetchall(
        "SELECT id, status, finished_at, last_error FROM jobs "
        "WHERE kind = 'backup' AND finished_at IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1"
    )
    last = finished[0] if finished else None
    failed = last is not None and (
        last["status"] == "failed"
        # Ambiguous means the job may have finished outside the worker's view;
        # its own file on disk settles that it did.
        or (last["status"] == "needs_attention" and not (latest and latest[1].endswith(f"-j{last['id']:020d}.db")))
    )
    overdue = latest is not None and now - latest[0] > timedelta(hours=BACKUP_OVERDUE_INTERVALS * interval_hours)

    status = {
        "latest_age": time_ago(latest[0], now) if latest else None,
        "latest_local": latest[0].astimezone().strftime("%Y-%m-%d %H:%M %Z") if latest else None,
        "failed_age": None,
        "failed_error": None,
    }
    if not enabled:
        status["state"] = "off"
    elif active and not overdue:
        status["state"] = "running"
    elif failed:
        status["state"] = "failed"
        status["failed_age"] = time_ago(datetime.fromisoformat(last["finished_at"]).replace(tzinfo=UTC), now)
        status["failed_error"] = last["last_error"] or None
    elif latest is None:
        status["state"] = "none"
    elif overdue:
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


PRE_IMPORT_MAX_COPIES = 3


async def snapshot_before_import(db) -> Path:
    """Copy of the account taken right before a JSON import replaces its data.

    Kept apart from the rotating backups, which an hourly schedule would prune
    within hours, and named to the second so a second import cannot replace it.
    """
    src_path = await db_main_path(db)
    directory = BACKUP_DIR / "pre-import"
    directory.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem
    dst = directory / f"{stem}-pre-import-{datetime.now(UTC).strftime('%Y-%m-%dT%H%M%S')}.db"
    await asyncio.to_thread(_publish_backup, src_path, dst, lambda _path: None)
    await asyncio.to_thread(_prune_backups, stem, PRE_IMPORT_MAX_COPIES, directory)
    logger.info("Pre-import snapshot created: %s", dst.name)
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
