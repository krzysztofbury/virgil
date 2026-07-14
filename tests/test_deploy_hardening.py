"""Round-3 hardening: webhook debounce race, pending-migration counting,
central/pre-migration backups, truncated-JSON repair."""

import asyncio
import sqlite3

import app.routers.oura_webhook as webhook_module
from app.services.llm import parse_andy_response


def test_debounce_is_race_free(monkeypatch, tmp_path):
    """N simultaneous deliveries must schedule exactly ONE sync — the old
    lock.locked() probe let all of them enqueue before any task started."""

    started = []

    async def fake_open(db_filename):
        return object()

    async def fake_close(db):
        return None

    async def fake_sync(db, days_back=2):
        started.append(1)
        await asyncio.sleep(0.02)
        return 0

    monkeypatch.setattr(webhook_module, "open_user_db", fake_open)
    monkeypatch.setattr(webhook_module, "close_user_db", fake_close)
    monkeypatch.setattr("app.services.oura_api.sync_oura_from_api", fake_sync)

    async def scenario():
        results = [webhook_module._schedule_user_sync("user-x.db", "sleep") for _ in range(5)]
        # Let the single scheduled task run to completion.
        await asyncio.sleep(0.1)
        # After completion a new sync may be scheduled again.
        again = webhook_module._schedule_user_sync("user-x.db", "sleep")
        await asyncio.sleep(0.1)
        return results, again

    results, again = asyncio.run(scenario())
    assert results == [True, False, False, False, False]
    assert again is True
    assert sum(started) == 2


def test_count_pending_migrations(tmp_path):
    async def scenario():
        import aiosqlite

        from app.migrations.runner import count_pending_migrations, run_migrations

        db = await aiosqlite.connect(tmp_path / "pending.db")
        db.row_factory = aiosqlite.Row
        fresh_pending = await count_pending_migrations(db)
        await run_migrations(db)
        after = await count_pending_migrations(db)
        await db.close()
        return fresh_pending, after

    fresh_pending, after = asyncio.run(scenario())
    assert fresh_pending >= 13, "an empty DB must report the full chain as pending"
    assert after == 0


def test_pre_migration_snapshot_versioned_and_never_overwritten(tmp_path, monkeypatch):
    import aiosqlite

    import app.services.backup as backup_module
    from app.migrations.runner import run_migrations
    from app.services.backup import snapshot_before_migration

    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

    async def scenario():
        db = await aiosqlite.connect(tmp_path / "snap.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        first = await snapshot_before_migration(db)
        pristine_bytes = first.read_bytes()

        # Simulate the retry-after-failed-migration case: the database mutates,
        # then a restart snapshots again at the SAME schema version — the
        # pristine copy must survive untouched.
        await db.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-01-01', 5)")
        await db.commit()
        second = await snapshot_before_migration(db)
        await db.close()
        return first, second, pristine_bytes

    first, second, pristine_bytes = asyncio.run(scenario())
    assert first == second, "same schema version must map to the same snapshot file"
    assert first.parent.name == "pre-migration", "snapshots live outside the rotating-prune namespace"
    assert "-pre-migration-v" in first.name
    assert first.read_bytes() == pristine_bytes, "snapshot was overwritten with a mutated database"
    copy = sqlite3.connect(first)
    try:
        n = copy.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        assert n >= 13, "snapshot must be a real copy of the migrated database"
    finally:
        copy.close()


def test_rotating_prune_never_touches_snapshots(tmp_path, monkeypatch):
    """Regression: `{stem}-*.db` also matched `{stem}-pre-migration-*` — the
    snapshots sorted as newest, filled every retention slot, and the prune
    then deleted every regular rotating backup."""
    import app.services.backup as backup_module
    from app.services.backup import _prune_backups

    backup_dir = tmp_path / "backups"
    snap_dir = backup_dir / "pre-migration"
    snap_dir.mkdir(parents=True)
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backup_dir)

    for hour in ("01", "02", "03", "04", "05"):
        (backup_dir / f"stem-2026-07-14T{hour}00.db").touch()
    (snap_dir / "stem-pre-migration-v013.db").touch()

    _prune_backups("stem", 3)

    kept = sorted(p.name for p in backup_dir.glob("stem-*.db"))
    assert kept == ["stem-2026-07-14T0300.db", "stem-2026-07-14T0400.db", "stem-2026-07-14T0500.db"]
    assert (snap_dir / "stem-pre-migration-v013.db").exists(), "prune must never see snapshots"


def test_central_backup_age_guard(tmp_path, monkeypatch):
    import app.services.backup as backup_module
    from app.services.backup import maybe_backup_central

    central = tmp_path / "virgil-central.db"
    conn = sqlite3.connect(central)
    conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backup_module, "CENTRAL_DB_PATH", str(central))
    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path / "backups")

    first = asyncio.run(maybe_backup_central())
    assert first is not None
    assert first.exists()

    # Fresh copy exists → the next call must be a no-op (once per 24h).
    second = asyncio.run(maybe_backup_central())
    assert second is None


def test_truncated_andy_json_repaired():
    """Thinking models truncate mid-object — salvage instead of failing."""
    truncated = '{\n "andy_body_desc": "Jump rope",\n "andy_relations_desc": "Book theater for Monika"'
    obj = parse_andy_response(truncated)
    assert obj["andy_relations_desc"] == "Book theater for Monika"

    truncated_mid_string = '{"andy_body_desc": "Jump rope", "andy_spirit_desc": "Stoic jour'
    obj = parse_andy_response(truncated_mid_string)
    assert obj["andy_body_desc"] == "Jump rope"


def test_complete_andy_json_still_parses():
    obj = parse_andy_response('prose before {"a": 1} prose after')
    assert obj == {"a": 1}
