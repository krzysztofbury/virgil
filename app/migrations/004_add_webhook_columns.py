"""Add webhook_secret column to integrations table."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(integrations)")
    col_names = [c[1] for c in cols]
    if "webhook_secret" not in col_names:
        await db.execute("ALTER TABLE integrations ADD COLUMN webhook_secret TEXT DEFAULT ''")
