"""Map a parsed movement name onto a training_exercises row.

A movement the user already trains resolves to their existing row untouched.
One that only appears in a WOD is created with ad_hoc = 1: it keeps its history,
volume and PB contribution, but never shows up in the daily protocol form.

A name absent from the CrossFit library resolves to None and creates nothing —
that is the guard that stops the exercise catalogue from filling with the
model's spelling variants.
"""

import logging

logger = logging.getLogger(__name__)


async def resolve_movement(db, name: str) -> int | None:
    """training_exercises.id for `name`, creating an ad-hoc row when needed."""
    clean = (name or "").strip()
    if not clean:
        return None

    existing = await db.execute_fetchall(
        "SELECT id FROM training_exercises WHERE lower(name) = lower(?) LIMIT 1", (clean,)
    )
    if existing:
        return existing[0]["id"]

    lib = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library "
        "WHERE category = 'CrossFit' AND lower(name) = lower(?) AND archived = 0 LIMIT 1",
        (clean,),
    )
    if not lib:
        logger.info("WOD movement %r is outside the CrossFit library — not created", clean)
        return None

    row = lib[0]
    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as m FROM training_exercises")
    next_order = (order_row[0]["m"] if order_row else 0) + 1
    cursor = await db.execute(
        "INSERT INTO training_exercises (name, section, metric, display_order, ad_hoc, notes) "
        "VALUES (?, ?, ?, ?, 1, 'Added from a WOD')",
        (row["name"], row["section"], row["metric"], next_order),
    )
    return cursor.lastrowid
