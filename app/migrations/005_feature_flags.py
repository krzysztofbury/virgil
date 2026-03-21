"""Seed first feature flag: feniks disabled by default."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('feature_feniks', '0')")
