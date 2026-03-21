"""Add source tracking columns to experiment tables for Oura integration."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(experiment_entries)")
    col_names = [c[1] for c in cols]
    if "source" not in col_names:
        await db.execute("ALTER TABLE experiment_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "source_ref" not in col_names:
        await db.execute("ALTER TABLE experiment_entries ADD COLUMN source_ref TEXT NOT NULL DEFAULT ''")

    cols = await db.execute_fetchall("PRAGMA table_info(experiment_activity_types)")
    col_names = [c[1] for c in cols]
    if "source_match" not in col_names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN source_match TEXT NOT NULL DEFAULT ''")

    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_entry_source "
        "ON experiment_entries(experiment_id, source, source_ref) WHERE source != 'manual'"
    )
