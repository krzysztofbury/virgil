"""Backfill the Air Squat movement into exercise_library.

Migration 016 seeded the CrossFit vocabulary with Back, Front and Overhead
Squat but no plain bodyweight squat. That vocabulary is closed - the WOD
parser's system prompt forbids guessing a near match, deliberately, so the
exercise catalogue cannot rot - which meant a note reading "15 squats" had
nothing to map to and came back as `unmatched`. Air Squat is a third of Cindy
and a fifth of Murph, so that gap cost the user most of any benchmark WOD they
logged.

`Air Squat` is now in CROSSFIT_MOVEMENTS, which covers fresh installs through
016. This migration is for databases already seeded without it.

INSERT OR IGNORE against UNIQUE(name COLLATE NOCASE): a no-op on a fresh
install (016 inserted it already) and on a database where the user added the
movement themselves, whose own metric/section/display_order choices must not be
overwritten.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def up(db: aiosqlite.Connection) -> None:
    cols = {c["name"] for c in await db.execute_fetchall("PRAGMA table_info(exercise_library)")}
    if not cols:
        # No exercise_library at all - a database this old predates 009 and has
        # no vocabulary to extend.
        logger.info("021: exercise_library absent - nothing to seed")
        return

    # 019 drops `category`; 016 still writes it. Support both so this migration
    # is correct whichever shape it lands on, rather than depending on the
    # ordering of a future backfill.
    columns = ["section", "name", "display_order", "metric"]
    values: list = ["Core", "Air Squat", 0, "reps"]
    if "category" in cols:
        columns.insert(0, "category")
        values.insert(0, "CrossFit")

    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) AS m FROM exercise_library")
    values[columns.index("display_order")] = (order_row[0]["m"] if order_row else 0) + 1

    placeholders = ",".join("?" * len(columns))
    await db.execute(
        f"INSERT OR IGNORE INTO exercise_library ({','.join(columns)}) VALUES ({placeholders})",  # noqa: S608
        values,
    )
    rows = await db.execute_fetchall("SELECT id, archived FROM exercise_library WHERE lower(name) = 'air squat'")
    if not rows:
        logger.warning("021: Air Squat still absent after INSERT OR IGNORE")
    else:
        logger.info("021: Air Squat present (id=%s, archived=%s)", rows[0]["id"], rows[0]["archived"])
