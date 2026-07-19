"""Generalize experiments + dictionary flags.

- experiment_activity_types: kind / target_value / target_period — a metric is
  now one of duration|count|boolean|scale, and count/boolean metrics carry
  their own target (per day/week/total).
- experiment_entries: generic `value` replaces duration_minutes (backfilled,
  then dropped — meaning depends on the metric's kind).
- exercise_library: builtin (seeded rows, protected from edit/delete) +
  archived (hidden from pickers, reversible).

Crash-retry safety: ALTER TABLE ADD COLUMN autocommits immediately under
aiosqlite's legacy isolation, while UPDATE/DROP ride the runner's transaction.
A crash between the two leaves the new column present but the data step
undone — so every data step is gated on its OWN completion marker
(duration_minutes still present; archived column still missing), never on the
added column's existence.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(experiment_activity_types)")
    names = {c[1] for c in cols}
    if "kind" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN kind TEXT NOT NULL DEFAULT 'duration'")
    if "target_value" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN target_value INTEGER NOT NULL DEFAULT 0")
    if "target_period" not in names:
        await db.execute("ALTER TABLE experiment_activity_types ADD COLUMN target_period TEXT NOT NULL DEFAULT 'week'")

    cols = await db.execute_fetchall("PRAGMA table_info(experiment_entries)")
    names = {c[1] for c in cols}
    if "value" not in names:
        await db.execute("ALTER TABLE experiment_entries ADD COLUMN value INTEGER NOT NULL DEFAULT 0")
    if "duration_minutes" in names:
        # Backfill + drop are keyed on duration_minutes existing (not on `value`
        # missing): a crash-retry after ADD COLUMN committed must still backfill.
        await db.execute("UPDATE experiment_entries SET value = duration_minutes")
        await db.execute("ALTER TABLE experiment_entries DROP COLUMN duration_minutes")

    cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
    names = {c[1] for c in cols}
    if "archived" not in names:
        # `archived` is added LAST and thus marks this whole block as completed.
        # Until then every row predates user-managed entries (no CRUD UI existed
        # before 015), so flagging all rows builtin on a crash-retry is correct.
        if "builtin" not in names:
            await db.execute("ALTER TABLE exercise_library ADD COLUMN builtin INTEGER NOT NULL DEFAULT 0")
        await db.execute("UPDATE exercise_library SET builtin = 1")
        await db.execute("ALTER TABLE exercise_library ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
