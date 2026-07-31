"""Exercise library: DB-backed, user-editable dictionary of exercises.

Seeded from app/exercise_library.py (seed data only — the DB is the
source of truth after this migration; edit rows, not the Python file).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            UNIQUE(category, name)
        )
        """
    )

    from app.exercise_library import EXERCISE_LIBRARY

    for order, ex in enumerate(EXERCISE_LIBRARY):
        await db.execute(
            "INSERT OR IGNORE INTO exercise_library (category, section, name, sets, reps, notes, display_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ex["category"], ex["section"], ex["name"], ex["sets"], ex["reps"], ex["notes"], order),
        )
    await db.commit()
