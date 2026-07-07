"""Add a per-exercise `metric` ('reps' vs 'time').

Type was derived from section, so the only section with a weight field (Core)
always treated the entered number as reps. A weighted hold/carry (Farmer's Walk,
Plank, Goblet Hold) logged as "60" (seconds) + 16 kg was counted as 60×16 kg of
volume — garbage. With a per-exercise metric, time exercises log weight+seconds
and are excluded from the kg-volume / total-reps aggregates (which self-heal:
past mis-entries drop out once their exercise is flagged 'time').
"""

import aiosqlite

# holds/carries whose rep spec may not end in 's'
_TIME_NAME_PATTERNS = ("%plank%", "%hold%", "%hang%", "%carry%", "%farmer%")


async def up(db: aiosqlite.Connection) -> None:
    for table in ("training_exercises", "exercise_library"):
        cols = await db.execute_fetchall(f"PRAGMA table_info({table})")
        if not any(c["name"] == "metric" for c in cols):
            await db.execute(f"ALTER TABLE {table} ADD COLUMN metric TEXT NOT NULL DEFAULT 'reps'")

    # reps column is named differently per table
    for table, reps_col in (("training_exercises", "target_reps"), ("exercise_library", "reps")):
        # time-like rep spec: ends in 's' ('45s', '30-45s') or contains 'min'
        await db.execute(f"UPDATE {table} SET metric = 'time' WHERE {reps_col} LIKE '%s' OR {reps_col} LIKE '%min%'")
        for pat in _TIME_NAME_PATTERNS:
            await db.execute(f"UPDATE {table} SET metric = 'time' WHERE lower(name) LIKE ?", (pat,))
