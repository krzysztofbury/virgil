"""Exercise library: DB-backed, user-editable dictionary of exercises.

Seeded from app/exercise_library.py (seed data only — the DB is the
source of truth after this migration; edit rows, not the Python file).

This table is created directly in its post-019 shape (no `category` column,
`UNIQUE(name)`, `exercise_library_tags` alongside it) so a brand-new database
never passes through the pre-019 shape at all — migration 019's own
"already migrated" check (absence of `category`) then correctly no-ops for a
fresh install. `metric`/`builtin`/`archived` are included here too (rather
than left to migrations 011/015 to ALTER ADD) for the same reason: those
migrations only add a column when it is missing, so seeding it here up front
means their ALTERs skip cleanly while their data-fixup logic (011's
rep-spec-derived `metric`) still runs unconditionally and still applies.

`builtin` is set to 1 explicitly for every row seeded here — migration 015
would otherwise be the one to flip pre-existing rows to builtin=1, but 015's
whole block is gated on `archived` being absent, which is no longer true once
this migration has run.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name)
        )
        """
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS exercise_library_tags (
            library_id INTEGER NOT NULL REFERENCES exercise_library(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (library_id, tag)
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_library_tags_tag ON exercise_library_tags(tag)")

    from app.exercise_library import EXERCISE_LIBRARY

    for order, ex in enumerate(EXERCISE_LIBRARY):
        await db.execute(
            "INSERT OR IGNORE INTO exercise_library (section, name, sets, reps, notes, display_order, builtin) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (ex["section"], ex["name"], ex["sets"], ex["reps"], ex["notes"], order),
        )
        row = await db.execute_fetchall("SELECT id FROM exercise_library WHERE name = ?", (ex["name"],))
        lib_id = row[0]["id"]
        for tag in ex.get("tags", []):
            await db.execute(
                "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) VALUES (?, ?)",
                (lib_id, tag),
            )
    await db.commit()
