"""Rename feature flag key feature_feniks -> feature_no_porn, preserving its value.

The module keeps the internal /feniks route and tables; only the user-facing name
and its feature-flag key change ('No Porn'). 005 still seeds feature_feniks='0' on
fresh DBs; this migration then renames it, so both fresh and existing DBs converge.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # Carry the old value over (INSERT OR IGNORE keeps an already-present new key), then drop the old.
    await db.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) "
        "SELECT 'feature_no_porn', value FROM app_settings WHERE key = 'feature_feniks'"
    )
    await db.execute("DELETE FROM app_settings WHERE key = 'feature_feniks'")
