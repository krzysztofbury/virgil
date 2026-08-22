"""No Porn module: bricks and a daily usage log.

Two additive tables; nothing existing changes shape:

- feniks_daily: one row per date — used (0/1), total minutes, edging (0/1).
  A day-based relapse counter under-measures prolonged sessions (hours log
  as one event), so the honest metric is a short daily log.
- feniks_bricks: urges survived, in Gola's brick structure (memory hook,
  craving 0-10, story). Bricks — not clean-day streaks — are the module's
  progress unit.

CREATE TABLE IF NOT EXISTS keeps this idempotent and safe to re-run; the same
DDL lives in app/db.py for fresh installs (this migration covers existing DBs).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS feniks_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
            minutes INTEGER,
            edging INTEGER NOT NULL DEFAULT 0 CHECK(edging IN (0, 1)),
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS feniks_bricks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            hook TEXT NOT NULL,
            craving INTEGER CHECK(craving BETWEEN 0 AND 10),
            story TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
