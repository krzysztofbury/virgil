import asyncio
import logging
import sqlite3
from datetime import date
from pathlib import Path

from app.config import DB_PATH
from app.db import get_setting

logger = logging.getLogger(__name__)

BACKUP_DIR = Path(DB_PATH).parent / "backups"


def _do_backup(src_path: str, dst_path: str) -> None:
    """Blocking SQLite backup via sqlite3.Connection.backup()."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _prune_backups(max_copies: int) -> None:
    """Remove oldest backups beyond max_copies."""
    if not BACKUP_DIR.exists():
        return
    backups = sorted(BACKUP_DIR.glob("virgil-*.db"), key=lambda p: p.name)
    while len(backups) > max_copies:
        oldest = backups.pop(0)
        oldest.unlink()
        logger.info("Pruned old backup: %s", oldest.name)


async def run_backup(db) -> Path:
    """Create a SQLite backup and prune old copies. Returns the backup path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    dst = BACKUP_DIR / f"virgil-{today}.db"

    await asyncio.to_thread(_do_backup, DB_PATH, str(dst))

    max_copies = int(await get_setting(db, "backup_max_copies", "7"))
    await asyncio.to_thread(_prune_backups, max_copies)

    logger.info("Backup created: %s", dst.name)
    return dst
