"""Backup service: per-user source path resolution, consistent copy, pruning."""

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

import app.services.backup as backup_module
from app.services.backup import _prune_backups, db_main_path, run_backup


def _make_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')")
    conn.execute("CREATE TABLE things (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO things(name) VALUES (?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()


def test_db_main_path_resolves_file(tmp_path):
    src = tmp_path / "user-abc.db"
    _make_source_db(src)

    async def run():
        db = await aiosqlite.connect(src)
        db.row_factory = aiosqlite.Row
        try:
            return await db_main_path(db)
        finally:
            await db.close()

    resolved = asyncio.run(run())
    assert Path(resolved).resolve() == src.resolve()


def test_run_backup_copies_user_db(tmp_path, monkeypatch):
    src = tmp_path / "user-xyz.db"
    _make_source_db(src)
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)

    async def run():
        db = await aiosqlite.connect(src)
        db.row_factory = aiosqlite.Row
        try:
            return await run_backup(db)
        finally:
            await db.close()

    dst = asyncio.run(run())
    assert dst.exists()
    assert dst.name.startswith("user-xyz-"), "backup filename must carry the source db stem"

    copy = sqlite3.connect(dst)
    count = copy.execute("SELECT COUNT(*) FROM things").fetchone()[0]
    copy.close()
    assert count == 3, "backup must contain the source data"


def test_prune_keeps_newest(tmp_path, monkeypatch):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)
    for day in ("01", "02", "03", "04", "05"):
        (backup_dir / f"stem-2026-07-{day}.db").touch()

    _prune_backups("stem", 3)

    left = sorted(p.name for p in backup_dir.glob("stem-*.db"))
    assert left == ["stem-2026-07-03.db", "stem-2026-07-04.db", "stem-2026-07-05.db"]
