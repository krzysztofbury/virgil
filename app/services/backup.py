import asyncio
import logging
import sqlite3
from datetime import date
from pathlib import Path

from app.config import CENTRAL_DB_PATH
from app.db import get_setting

logger = logging.getLogger(__name__)

# Anchored to the central DB dir (the mounted /data volume in Docker) so
# backups survive container rebuilds. The legacy DB_PATH pointed inside
# the image in prod, silently backing up a nonexistent (empty) database.
BACKUP_DIR = Path(CENTRAL_DB_PATH).parent / "backups"


def _do_backup(src_path: str, dst_path: str) -> None:
    """Blocking SQLite backup via sqlite3.Connection.backup()."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _prune_backups(stem: str, max_copies: int) -> None:
    """Remove oldest backups for one database beyond max_copies."""
    if not BACKUP_DIR.exists():
        return
    backups = sorted(BACKUP_DIR.glob(f"{stem}-*.db"), key=lambda p: p.name)
    while len(backups) > max_copies:
        oldest = backups.pop(0)
        oldest.unlink()
        logger.info("Pruned old backup: %s", oldest.name)


async def db_main_path(db) -> str:
    """Resolve the on-disk path of the connection's main database."""
    rows = await db.execute_fetchall("PRAGMA database_list")
    for row in rows:
        if row["name"] == "main" and row["file"]:
            return row["file"]
    raise RuntimeError("Cannot back up: main database has no file path (in-memory?)")


async def run_backup(db) -> Path:
    """Create a consistent SQLite backup of THIS connection's database and prune old copies.

    The source path is derived from the connection itself (PRAGMA database_list),
    so per-user databases are backed up correctly — never the legacy global DB_PATH.
    """
    src_path = await db_main_path(db)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(src_path).stem
    today = date.today().isoformat()
    dst = BACKUP_DIR / f"{stem}-{today}.db"

    await asyncio.to_thread(_do_backup, src_path, str(dst))

    max_copies = int(await get_setting(db, "backup_max_copies", "7"))
    await asyncio.to_thread(_prune_backups, stem, max_copies)

    logger.info("Backup created: %s", dst.name)
    return dst
