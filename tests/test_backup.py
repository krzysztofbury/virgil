"""Backup service: per-user source path resolution, consistent copy, pruning."""

import asyncio
import os
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

import app.services.backup as backup_module
from app.services.backup import _prune_backups, db_main_path, run_backup


def test_corrupt_recent_central_backup_is_replaced(tmp_path, monkeypatch):
    source = tmp_path / "central.db"
    _make_source_db(source)
    directory = tmp_path / "backups"
    directory.mkdir()
    corrupt = directory / "central-2026-10-11T1200.db"
    corrupt.write_bytes(b"interrupted snapshot")
    monkeypatch.setattr(backup_module, "CENTRAL_DB_PATH", str(source))
    monkeypatch.setattr(backup_module, "BACKUP_DIR", directory)

    result = asyncio.run(backup_module.maybe_backup_central())

    assert result is not None
    with sqlite3.connect(result) as db:
        assert db.execute("SELECT name FROM things ORDER BY id").fetchall() == [("a",), ("b",), ("c",)]
    assert asyncio.run(backup_module.maybe_backup_central()) is None


def test_interrupted_central_backup_is_never_published(tmp_path, monkeypatch):
    source = tmp_path / "central.db"
    _make_source_db(source)
    directory = tmp_path / "backups"
    monkeypatch.setattr(backup_module, "CENTRAL_DB_PATH", str(source))
    monkeypatch.setattr(backup_module, "BACKUP_DIR", directory)

    def interrupted(_source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("interrupted")

    monkeypatch.setattr(backup_module, "_do_backup", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        asyncio.run(backup_module.maybe_backup_central())
    assert list(directory.iterdir()) == []


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


def test_prune_orders_fixed_width_job_suffixes_numerically(tmp_path):
    for job_id in (9, 10):
        (tmp_path / f"stem-2026-07-05T1200-j{job_id:020d}.db").touch()

    _prune_backups("stem", 1, directory=tmp_path)

    assert [path.name for path in tmp_path.glob("stem-*.db")] == ["stem-2026-07-05T1200-j00000000000000000010.db"]


def _volumes(tmp_path, monkeypatch):
    data = tmp_path / "data" / "backups"
    mount = tmp_path / "mount"
    monkeypatch.setattr(backup_module, "DATA_VOLUME_BACKUP_DIR", data)
    monkeypatch.setattr(backup_module, "BACKUP_DIR", mount)
    return data, mount


def _files(root) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())


def test_backups_left_on_the_data_volume_move_to_the_backup_mount(tmp_path, monkeypatch):
    data, mount = _volumes(tmp_path, monkeypatch)
    (data / "pre-migration").mkdir(parents=True)
    _make_source_db(data / "user-2026-10-01T0000.db")
    os.utime(data / "user-2026-10-01T0000.db", (1_700_000_000, 1_700_000_000))
    (data / "pre-migration" / "user-pre-migration-v036.db").write_bytes(b"pristine")
    # A move that stopped after its copy but before deleting the source.
    (data / "user-2026-10-02T0000.db").write_bytes(b"moved")
    mount.mkdir()
    (mount / "user-2026-10-02T0000.db").write_bytes(b"moved")

    assert backup_module.relocate_data_volume_backups() == 3

    assert _files(data) == []
    assert _files(mount) == [
        "pre-migration/user-pre-migration-v036.db",
        "user-2026-10-01T0000.db",
        "user-2026-10-02T0000.db",
    ]
    with sqlite3.connect(mount / "user-2026-10-01T0000.db") as moved:
        assert moved.execute("SELECT name FROM things ORDER BY id").fetchall() == [("a",), ("b",), ("c",)]
    # Settings reads a backup's age from this.
    assert (mount / "user-2026-10-01T0000.db").stat().st_mtime == 1_700_000_000
    assert (mount / "pre-migration" / "user-pre-migration-v036.db").read_bytes() == b"pristine"
    assert backup_module.relocate_data_volume_backups() == 0


def test_a_different_file_under_the_same_name_keeps_both(tmp_path, monkeypatch):
    data, mount = _volumes(tmp_path, monkeypatch)
    data.mkdir(parents=True)
    mount.mkdir()
    (data / "user-2026-10-02T0000.db").write_bytes(b"the good copy")
    (mount / "user-2026-10-02T0000.db").write_bytes(b"cut short")

    assert backup_module.relocate_data_volume_backups() == 0

    assert (data / "user-2026-10-02T0000.db").read_bytes() == b"the good copy"
    assert (mount / "user-2026-10-02T0000.db").read_bytes() == b"cut short"


def test_one_directory_under_two_names_loses_nothing(tmp_path, monkeypatch):
    """One host folder mounted at both /data/backups and /backups: a "move" from
    one name to the other would delete the only copy."""
    data, mount = _volumes(tmp_path, monkeypatch)
    (data / "pre-migration").mkdir(parents=True)
    (data / "user-2026-10-01T0000.db").write_bytes(b"only copy")
    (data / "pre-migration" / "user-pre-migration-v036.db").write_bytes(b"only snapshot")
    # A symlink resolves away; the second spelling here is what a bind mount gives.
    mount.symlink_to(data, target_is_directory=True)
    monkeypatch.setattr(backup_module.Path, "resolve", lambda self, strict=False: self.absolute())

    assert backup_module.relocate_data_volume_backups() == 0

    assert _files(data) == ["pre-migration/user-pre-migration-v036.db", "user-2026-10-01T0000.db"]


def test_one_file_under_two_names_is_left_alone(tmp_path, monkeypatch):
    """A nested mount can make one file appear in both directories."""
    data, mount = _volumes(tmp_path, monkeypatch)
    data.mkdir(parents=True)
    mount.mkdir()
    (data / "user-2026-10-01T0000.db").write_bytes(b"only copy")
    os.link(data / "user-2026-10-01T0000.db", mount / "user-2026-10-01T0000.db")

    assert backup_module.relocate_data_volume_backups() == 0

    assert (mount / "user-2026-10-01T0000.db").read_bytes() == b"only copy"


@pytest.mark.parametrize("inside", ["mount-in-data", "data-in-mount"])
def test_a_backup_mount_overlapping_the_data_backups_is_refused(tmp_path, monkeypatch, inside):
    data, _mount = _volumes(tmp_path, monkeypatch)
    if inside == "mount-in-data":
        monkeypatch.setattr(backup_module, "BACKUP_DIR", data / "offsite")
    else:
        monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "data")
    data.mkdir(parents=True)
    (data / "user-2026-10-01T0000.db").write_bytes(b"copy")

    with pytest.raises(RuntimeError, match="must not overlap"):
        backup_module.relocate_data_volume_backups()
    assert (data / "user-2026-10-01T0000.db").read_bytes() == b"copy"


def test_a_copy_that_fails_leaves_the_source(tmp_path, monkeypatch):
    data, mount = _volumes(tmp_path, monkeypatch)
    data.mkdir(parents=True)
    (data / "user-2026-10-01T0000.db").write_bytes(b"only copy")

    def disk_full(_source, destination):
        Path(destination).write_bytes(b"part")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup_module.shutil, "copy2", disk_full)
    with pytest.raises(OSError, match="No space left"):
        backup_module.relocate_data_volume_backups()

    assert (data / "user-2026-10-01T0000.db").read_bytes() == b"only copy"
    assert _files(mount) == []


def test_deleting_an_account_also_removes_its_copies_still_on_the_data_volume(tmp_path, monkeypatch):
    data, mount = _volumes(tmp_path, monkeypatch)
    stem = "22222222-2222-4222-8222-222222222222"
    other = "33333333-3333-4333-8333-333333333333"
    for root in (data, mount):
        (root / "pre-migration").mkdir(parents=True)
        (root / f"{stem}-2026-10-01T0000.db").write_bytes(b"")
        (root / "pre-migration" / f"{stem}-pre-migration-v036.db").write_bytes(b"")
        (root / f".{stem}-2026-10-02T0000.db.0123abcd.tmp").write_bytes(b"cut short")
        (root / "pre-migration" / f".{stem}-pre-migration-v037.db.0123abcd.tmp").write_bytes(b"")
        (root / f"{other}-2026-10-01T0000.db").write_bytes(b"")

    backup_module.delete_user_backups(f"{stem}.db")

    assert _files(tmp_path) == [f"data/backups/{other}-2026-10-01T0000.db", f"mount/{other}-2026-10-01T0000.db"]


@pytest.mark.parametrize("cause", ["read-only", "no-hard-links"])
def test_a_backup_mount_that_cannot_hold_backups_is_named_at_startup(tmp_path, monkeypatch, cause):
    mount = tmp_path / "mount"
    mount.mkdir()
    monkeypatch.setattr(backup_module, "BACKUP_DIR", mount)
    assert backup_module.backup_dir_problem() is None

    if cause == "read-only":
        if os.getuid() == 0:
            pytest.skip("root writes through any mode")
        mount.chmod(0o555)
    else:
        # What an exFAT or FAT disk answers.
        def refuse(*_args):
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(backup_module.os, "link", refuse)
    try:
        problem = backup_module.backup_dir_problem()
    finally:
        mount.chmod(0o755)

    assert problem is not None
    assert str(mount) in problem
    assert list(mount.iterdir()) == []
