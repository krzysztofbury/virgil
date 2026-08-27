"""Add training_sessions.capture_token so a replayed capture claims one session.

POST /training/wod commits the session before it calls the LLM, by design: a
parse failure must never cost the raw note. That made a double click create two
sessions and pay for two parses, and the first session then counted toward the
weekly KPI with no entries.

The token comes from the form that GET /training rendered. A partial unique
index enforces one session per token, and existing rows keep NULL, which the
index ignores. A row written for a reused token with different content stores
NULL as well, so it collides with nothing.

Crash-retry safety (per the runner's semantics): both statements carry their own
guard, so a re-run after a crash is a no-op.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = {c[1] for c in await db.execute_fetchall("PRAGMA table_info(training_sessions)")}
    if cols and "capture_token" not in cols:
        await db.execute("ALTER TABLE training_sessions ADD COLUMN capture_token TEXT")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_capture_token "
        "ON training_sessions(capture_token) WHERE capture_token IS NOT NULL"
    )
