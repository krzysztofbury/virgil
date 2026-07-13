"""Add training_exercises.archived — deleting an exercise must not erase history.

Deleting an exercise used to cascade-delete every historical training entry and
personal best that referenced it. Exercises with logged entries are now archived
(hidden from the protocol/log forms) instead of hard-deleted.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(training_exercises)")
    if not any(c["name"] == "archived" for c in cols):
        await db.execute("ALTER TABLE training_exercises ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
