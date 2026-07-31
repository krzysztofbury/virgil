"""Map a parsed movement name onto a training_exercises row.

A movement the user already trains resolves to their existing row untouched.
One that only appears in a WOD is created with ad_hoc = 1: it keeps its history,
volume and PB contribution, but never shows up in the daily protocol form.

A name absent from the exercise library resolves to None and creates nothing —
that is the guard that stops the exercise catalogue from filling with the
model's spelling variants.

M1 (2026-07-30 review): an existing training_exercises row that happens to be
archived is still matched and reused, not skipped — and is un-archived in the
process. The user just logged a workout containing this movement, so "retired"
is no longer an accurate description of it; leaving it archived while a fresh
WOD keeps feeding its Volume/PB aggregates (neither of which filter on
`archived`) is the incoherent state the review flagged. Reusing the row
without un-archiving it would silently reattach history to a row the protocol
form still hides, with no way for the user to notice.
"""

import logging

logger = logging.getLogger(__name__)


async def resolve_movement(db, name: str) -> int | None:
    """training_exercises.id for `name`, creating an ad-hoc row when needed."""
    clean = (name or "").strip()
    if not clean:
        return None

    existing = await db.execute_fetchall(
        "SELECT id, archived FROM training_exercises WHERE lower(name) = lower(?) LIMIT 1", (clean,)
    )
    if existing:
        ex_id = existing[0]["id"]
        if existing[0]["archived"]:
            await db.execute("UPDATE training_exercises SET archived = 0 WHERE id = ?", (ex_id,))
            logger.info(
                "WOD movement %r matched an archived training_exercises row (id=%s) — reactivating it", clean, ex_id
            )
        return ex_id

    # Same dedupe tie-break as canonical_movements() in wod_parser.py:
    # exercise_library.name is UNIQUE(name COLLATE NOCASE) (migration 019)
    # and validate_library_write's dup checks are case-insensitive too, so a
    # duplicate can no longer be created through the app — the
    # lowest-display_order ORDER BY is defense in depth only, against a
    # hand-edited or otherwise malformed database. Both functions must agree,
    # or a movement resolves to a different section/metric depending on which
    # one is asked; see canonical_movements()'s docstring for the full
    # rationale.
    lib = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library "
        "WHERE lower(name) = lower(?) AND archived = 0 "
        "ORDER BY display_order LIMIT 1",
        (clean,),
    )
    if not lib:
        logger.info("WOD movement %r is outside the exercise library — not created", clean)
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
