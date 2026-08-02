"""Normalise training_entries.duration to seconds.

Two different writers filled this column with two different units.

`save_session` (the per-set log form, deleted 2026-08-01) rendered a "min" input
for Warmup, Cardio and Stretching and validated it against DURATION_MINUTES_MAX,
so those rows are **minutes**. Its one exception was Core + metric='time', which
had a "sec" input validated against DURATION_SECONDS_MAX — **seconds**.

`confirm_wod`, now the only writer, always stores **seconds**.

The training page printed the raw value with a literal " min", so a 69-minute
ride stored as 4140 rendered as "4140.0 min" while the same session's header —
`training_sessions.duration_minutes`, typed by hand — correctly said "69 min".
That inconsistency is what surfaced this.

Seconds wins as the canonical unit: it is what the parser produces, what the
confirm screen labels ("Czas (s)"), and what the only remaining writer stores.

## Why the rule below, and not a magnitude guess

A duration in minutes is realistically 0.5–120 and one in seconds 30–7200, so
the two overlap across 30–120: `45` is equally a 45-second plank or a 45-minute
swim. Guessing from the value would be a proxy for the real predicate, and this
branch has already paid for that mistake several times over.

The rule is structural instead — it reproduces exactly which branch of the old
writer produced each row:

    Warmup (any metric)      -> minutes   (its "min" input ignored metric)
    Cardio + metric='reps'   -> minutes   (the rounds+duration branch)
    Cardio + metric='time'   -> seconds   (only reachable via confirm_wod)
    Core   + metric='time'   -> seconds   (the old writer's one seconds branch)
    Stretching               -> minutes   (its "min" input)

Verified against the deployed database on 2026-08-02, all 35 rows carrying a
duration. Two independent sources confirm the split rather than assuming it:
the user's own written program prescribes "Farmer's walk 3x30-45 s" and "Plank
3x45 s", and those rows hold 50-60 and 30-45 — correct as seconds; while the
WOD notes say "1 minute running" beside a stored 60, and "69 minutes" beside a
stored 4140 — also correct as seconds. Every row the rule converts lands on a
plausible value (60-3000 s) and every row it leaves alone reads plausibly as
minutes (0.5-69).

## Why the id and date ceilings

"Warmup is minutes" describes the rows that exist now. It is not true going
forward: a WOD note mentioning a warm-up movement writes a Warmup row in
seconds. The ceilings below bound this to the rows verified above, so anything
captured between that verification and this migration running is left alone.
id 103 is the newest legacy row; 121 is already a WOD row and must not move.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

# Rows verified on 2026-08-02; see the module docstring.
MAX_LEGACY_ENTRY_ID = 103
MAX_LEGACY_SESSION_DATE = "2026-07-30"

_LEGACY_MINUTES = """
    SELECT te.id
    FROM training_entries te
    JOIN training_exercises tex ON te.exercise_id = tex.id
    JOIN training_sessions ts ON te.session_id = ts.id
    WHERE te.duration IS NOT NULL
      AND te.duration > 0
      AND te.id <= ?
      AND ts.date <= ?
      AND (
            tex.section IN ('Warmup', 'Stretching')
         OR (tex.section = 'Cardio' AND tex.metric = 'reps')
      )
"""


async def up(db: aiosqlite.Connection) -> None:
    # A fresh install has nothing to convert and must not error — the count is
    # logged rather than asserted for exactly that reason.
    cols = await db.execute_fetchall("PRAGMA table_info(training_exercises)")
    if not any(c["name"] == "metric" for c in cols):
        # Pre-011 schema: no metric column, so the rule cannot be evaluated.
        # Such a database also predates every row this migration targets.
        logger.info("020: training_exercises.metric absent — nothing to convert")
        return

    rows = await db.execute_fetchall(_LEGACY_MINUTES, (MAX_LEGACY_ENTRY_ID, MAX_LEGACY_SESSION_DATE))
    ids = [r["id"] for r in rows]
    if not ids:
        logger.info("020: no legacy minute-valued durations found")
        return

    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE training_entries SET duration = duration * 60 WHERE id IN ({placeholders})",  # noqa: S608
        ids,
    )
    logger.info("020: converted %d duration values from minutes to seconds: %s", len(ids), ids)
