"""No Porn single-flow redesign: reconcile the tables migration 022 shipped.

Migration 022 was released on main with the first-iteration shape (feniks_daily
without note; feniks_bricks with situation/action/lesson) and may already have
run on existing DBs, so it must not be edited. Fresh installs get the final
shape straight from app/db.py, which makes 022 a no-op there. This migration
closes the gap for DBs that ran the released 022:

- feniks_daily gains `note` (one-line trigger/feeling).
- feniks_bricks rebuilds to hook/craving/story, merging the old free-text
  fields (situation, action, lesson) into `story` so nothing captured is lost.

Crash-retry safety (per the runner's semantics): ALTER/CREATE/DROP autocommit
immediately under aiosqlite's legacy isolation, so every step is gated on its
OWN completion marker (column presence), never on a previous step's side
effect, and the scratch table is dropped up-front in case a prior attempt
crashed between creating it and the rename.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = {c[1] for c in await db.execute_fetchall("PRAGMA table_info(feniks_daily)")}
    if cols and "note" not in cols:
        await db.execute("ALTER TABLE feniks_daily ADD COLUMN note TEXT DEFAULT ''")

    cols = {c[1] for c in await db.execute_fetchall("PRAGMA table_info(feniks_bricks)")}
    if cols and "story" not in cols:
        await db.execute("DROP TABLE IF EXISTS feniks_bricks_new")
        await db.execute(
            """
            CREATE TABLE feniks_bricks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                hook TEXT NOT NULL,
                craving INTEGER CHECK(craving BETWEEN 0 AND 10),
                story TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        await db.execute(
            """
            INSERT INTO feniks_bricks_new (id, date, hook, craving, story, created_at)
            SELECT id, date, hook, craving,
                   TRIM(
                       COALESCE(situation, '')
                       || CASE WHEN COALESCE(action, '') != '' THEN char(10) || action ELSE '' END
                       || CASE WHEN COALESCE(lesson, '') != '' THEN char(10) || lesson ELSE '' END,
                       char(10)
                   ),
                   created_at
            FROM feniks_bricks
            """
        )
        await db.execute("DROP TABLE feniks_bricks")
        await db.execute("ALTER TABLE feniks_bricks_new RENAME TO feniks_bricks")
