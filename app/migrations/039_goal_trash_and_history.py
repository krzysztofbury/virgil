"""Let a deleted goal come back for a while, and keep a short history per goal.

A deleted goal was gone for good, and an edit left no trace. `deleted_goals`
holds a deleted goal's row, and the goals and experiments its delete unlinked,
until it is restored or ages out. `goal_changes` records what happened to a goal
and which fields changed, never their values: the goal's text stays in one place.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS deleted_goals (
            id INTEGER PRIMARY KEY,
            goal_json TEXT NOT NULL,
            child_goal_ids TEXT NOT NULL DEFAULT '[]',
            experiment_ids TEXT NOT NULL DEFAULT '[]',
            deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS goal_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            goal_id INTEGER NOT NULL,
            changed_at TEXT NOT NULL DEFAULT (datetime('now')),
            event TEXT NOT NULL CHECK(event IN ('created', 'edited', 'focused', 'unfocused', 'deleted', 'restored')),
            fields TEXT NOT NULL DEFAULT '',
            status_from TEXT,
            status_to TEXT
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_goal_changes_goal ON goal_changes(goal_id, id)")
