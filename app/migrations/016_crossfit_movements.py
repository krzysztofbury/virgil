"""Add training_exercises.ad_hoc and seed the CrossFit movement vocabulary.

ad_hoc marks a movement the WOD parser created on demand rather than one the
user put on their standing protocol. Ad-hoc movements stay out of the protocol
form (training.py) but remain visible to session history, weekly volume and
personal bests — those queries join by id and filter neither flag.

`archived` is deliberately not reused for this: it means *retired* (013), and
overloading it would make a withdrawn exercise indistinguishable from one
logged ad hoc.

The CrossFit rows are the closed vocabulary the WOD parser is constrained to.
exercise_library is UNIQUE(name) (migration 009, in its post-019 shape from
the start) — a name already seeded by 009 (Back Squat, Deadlift, Bench Press,
Pull-up all exist in both EXERCISE_LIBRARY and CROSSFIT_MOVEMENTS) collides on
INSERT. The upsert below resolves that the same way migration 019 resolves it
for a pre-existing database: the CrossFit metric wins (it is written
explicitly here, where 009's version was DERIVED by migration 011 from the
rep-spec string) and the row becomes user-editable (builtin=0), since the
CrossFit vocabulary must stay editable regardless of which row got there
first — see migration 017. Re-running is safe either way (upsert, not
INSERT-or-fail).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(training_exercises)")
    if not any(c["name"] == "ad_hoc" for c in cols):
        await db.execute("ALTER TABLE training_exercises ADD COLUMN ad_hoc INTEGER NOT NULL DEFAULT 0")

    from app.exercise_library import CROSSFIT_MOVEMENTS

    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as m FROM exercise_library")
    base_order = order_row[0]["m"] if order_row else 0

    for offset, ex in enumerate(CROSSFIT_MOVEMENTS, start=1):
        # Direct indexing (not .get()) ensures missing keys raise loudly at migration
        # time, catching future bugs. This structure prevents silent mis-typing:
        # a movement added without an explicit metric would default to 'reps',
        # recreating the exact defect that motivated the CROSSFIT_MOVEMENTS separation.
        # builtin = 0 on purpose: app/library_validation.py's validate_library_write
        # guards update/delete of a builtin row to 'archived' only.
        # This vocabulary is the user's to curate — add, rename, delete.
        await db.execute(
            "INSERT INTO exercise_library "
            "(section, name, sets, reps, notes, display_order, metric, builtin) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(name) DO UPDATE SET metric = excluded.metric, builtin = 0",
            (
                ex["section"],
                ex["name"],
                ex["sets"],
                ex["reps"],
                ex["notes"],
                base_order + offset,
                ex["metric"],
            ),
        )
        row = await db.execute_fetchall("SELECT id FROM exercise_library WHERE name = ?", (ex["name"],))
        lib_id = row[0]["id"]
        for tag in ex.get("tags", []):
            await db.execute(
                "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) VALUES (?, ?)",
                (lib_id, tag),
            )
