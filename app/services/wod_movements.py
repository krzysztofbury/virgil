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

    # Unlike canonical_movements()'s unscoped full-table scan in
    # wod_parser.py, this WHERE targets one specific name, so no tie-break is
    # needed here even though exercise_library.name's UNIQUE(name COLLATE
    # NOCASE) (migration 019) is ASCII-only and can't stop two rows differing
    # only in a non-ASCII letter's case ('Ćwiczenie' vs 'ćwiczenie') from both
    # existing (confirmed: SQLite lets that second INSERT through — see
    # test_non_ascii_duplicate_names_each_resolve_unambiguously below). What
    # that leaves is not ambiguity, though: `lower(name) = lower(?)` compares
    # both sides with the SAME SQL lower() the UNIQUE constraint is built on,
    # so if two rows both matched a given `clean`, they would — by
    # transitivity of equality — also match each other's lower(name), which
    # is exactly what UNIQUE(name COLLATE NOCASE) already forbids. So at most
    # one row can ever satisfy this WHERE clause, for any `clean`, whether or
    # not a Unicode-only duplicate of it exists elsewhere in the table —
    # there is nothing left to order or limit among.
    lib = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library WHERE lower(name) = lower(?) AND archived = 0",
        (clean,),
    )
    if not lib:
        logger.info("WOD movement %r is outside the exercise library — not created", clean)
        return None

    row = lib[0]
    # ad_hoc = 1 below has no reader today, and that is a decision, not an
    # oversight. It records provenance: this training_exercises row came from a
    # note, not from the user's curated dictionary. The Settings listing reads
    # exercise_library, not this table, so nothing surfaces the flag - do not
    # read that as dead weight and drop the column without deciding how
    # provenance gets recorded instead.
    #
    # `archived` on this table is the same shape in reverse: this module only
    # ever CLEARS it (above, when a logged movement turns out to be archived).
    # Setting it happens in Settings, on the exercise_library row. Two tables,
    # two flags, one direction each.
    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as m FROM training_exercises")
    next_order = (order_row[0]["m"] if order_row else 0) + 1
    cursor = await db.execute(
        """INSERT INTO training_exercises (name, section, metric, display_order, ad_hoc, notes)
           VALUES (?, ?, ?, ?, 1, 'Added from a WOD')
           ON CONFLICT(name COLLATE NOCASE) DO UPDATE SET archived = 0
           RETURNING id""",
        (row["name"], row["section"], row["metric"], next_order),
    )
    created_or_existing = await cursor.fetchone()
    assert created_or_existing is not None, "movement upsert must return an exercise id"
    return created_or_existing["id"]
