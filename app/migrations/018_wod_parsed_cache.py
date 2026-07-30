"""Add training_sessions.wod_parsed — cache the WOD parser result for Post/Redirect/Get.

POST /training/wod used to render the confirmation screen directly (200 HTML),
so replaying the POST (double-click, or the browser's "confirm resubmission" on
a refresh) created a second session and fired a second paid LLM call. Worse: an
F5 mid-review created session #2, the confirm form silently rebound to it, and
session #1 survived entry-less — still counted in the weekly kpi_sessions KPI
and still shown in history.

This column holds the JSON of {entries, unmatched, parse_error} computed once
by POST /training/wod, which now 303-redirects to
GET /training/wod/confirm/{session_id}. That GET reads this column back and
renders — it must NEVER re-invoke the parser, or a page refresh would cost
another LLM call.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(training_sessions)")
    if not any(c["name"] == "wod_parsed" for c in cols):
        await db.execute("ALTER TABLE training_sessions ADD COLUMN wod_parsed TEXT")
