"""Flip backup_enabled to ON for existing databases.

The seed default changed to '1', but seeds only run inside migration 001 —
every existing install still carries the old seeded '0' row, which masks the
new default forever. Deliberate policy change: backups become opt-out, and
this overrides a manual opt-out too (we cannot distinguish "seeded 0" from
"user chose 0"; losing months of health data is the worse failure).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("UPDATE app_settings SET value = '1' WHERE key = 'backup_enabled'")
