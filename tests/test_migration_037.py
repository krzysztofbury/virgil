"""Migration 037: nothing the Second Brain folder left behind stays queued or configured."""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


def test_export_jobs_and_settings_are_removed_and_other_work_stays(tmp_path):
    async def scenario():
        db = await aiosqlite.connect(tmp_path / "upgrade.db")
        db.row_factory = aiosqlite.Row
        try:
            for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
                if int(re.match(r"\d{3}", file.name).group()) > 36:
                    continue
                await importlib.import_module(f"app.migrations.{file.stem}").up(db)
                await db.commit()
            await db.executemany(
                "INSERT INTO jobs(kind, payload_json, idempotency_key) VALUES(?, ?, ?)",
                [
                    ("markdown_export", '{"scope":"weekly","sections":null,"trigger":"scheduled"}', "export:1"),
                    ("backup", '{"trigger":"manual"}', "backup:1"),
                ],
            )
            await db.executemany(
                "INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)",
                [("export_enabled", "1"), ("export_interval_hours", "6"), ("backup_enabled", "1")],
            )
            await db.commit()
            migration = importlib.import_module("app.migrations.037_drop_second_brain")
            for _ in range(2):
                await migration.up(db)
                await db.commit()
            jobs = [row["kind"] for row in await db.execute_fetchall("SELECT kind FROM jobs")]
            keys = {
                row["key"]
                for row in await db.execute_fetchall(
                    "SELECT key FROM app_settings WHERE key IN ('export_enabled', 'export_interval_hours', 'backup_enabled')"
                )
            }
            return jobs, keys
        finally:
            await db.close()

    jobs, keys = asyncio.run(scenario())
    assert jobs == ["backup"]
    assert keys == {"backup_enabled"}
