"""Add goals.active so a current-focus set can exist.

The Goals page rendered every area against every horizon, which made "what am I
working on now" unanswerable and produced 24 identical inputs in the empty
state. The flag is advisory: the page warns above three active goals and blocks
nothing, so an upgrade can never drop a goal to satisfy a cap.

Crash-retry safety (per the runner's semantics): the ALTER is gated on the
column's own absence, so a re-run after a crash is a no-op.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = {c[1] for c in await db.execute_fetchall("PRAGMA table_info(goals)")}
    if cols and "active" not in cols:
        await db.execute("ALTER TABLE goals ADD COLUMN active INTEGER NOT NULL DEFAULT 0")
