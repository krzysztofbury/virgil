"""Add training_exercises.ad_hoc and seed the CrossFit movement vocabulary.

ad_hoc marks a movement the WOD parser created on demand rather than one the
user put on their standing protocol. Ad-hoc movements stay out of the protocol
form (training.py) but remain visible to session history, weekly volume and
personal bests — those queries join by id and filter neither flag.

`archived` is deliberately not reused for this: it means *retired* (013), and
overloading it would make a withdrawn exercise indistinguishable from one
logged ad hoc.

The CrossFit rows are the closed vocabulary the WOD parser is constrained to.
Seeding is INSERT OR IGNORE against UNIQUE(category, name), so re-running is safe
and a user's own edits to a row are never overwritten.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(training_exercises)")
    if not any(c["name"] == "ad_hoc" for c in cols):
        await db.execute("ALTER TABLE training_exercises ADD COLUMN ad_hoc INTEGER NOT NULL DEFAULT 0")

    from app.exercise_library import CROSSFIT_MOVEMENTS

    crossfit = CROSSFIT_MOVEMENTS
    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as m FROM exercise_library")
    base_order = order_row[0]["m"] if order_row else 0

    for offset, ex in enumerate(crossfit, start=1):
        await db.execute(
            "INSERT OR IGNORE INTO exercise_library "
            "(category, section, name, sets, reps, notes, display_order, metric, builtin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                ex["category"],
                ex["section"],
                ex["name"],
                ex.get("sets"),
                ex.get("reps", ""),
                ex.get("notes", ""),
                base_order + offset,
                ex.get("metric", "reps"),
            ),
        )
