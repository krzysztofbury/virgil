"""Resolving a parsed movement name to a training_exercises row."""

import asyncio

import aiosqlite

from app.services.wod_movements import resolve_movement


async def _db(tmp_path):
    db = await aiosqlite.connect(tmp_path / "u.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE training_exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            section TEXT NOT NULL,
            target_sets INTEGER,
            target_reps TEXT,
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            archived INTEGER NOT NULL DEFAULT 0,
            ad_hoc INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        """CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name COLLATE NOCASE)
        )"""
    )
    await db.execute(
        "INSERT INTO exercise_library (section, name, metric, builtin) VALUES ('Core', 'Thruster', 'reps', 1)"
    )
    await db.execute(
        "INSERT INTO exercise_library (section, name, metric, builtin) VALUES ('Cardio', 'Row', 'time', 1)"
    )
    await db.execute("INSERT INTO training_exercises (name, section) VALUES ('Goblet Squat', 'Core')")
    await db.commit()
    return db


def test_creates_ad_hoc_row_inheriting_section_and_metric(tmp_path):
    async def run():
        db = await _db(tmp_path)
        try:
            ex_id = await resolve_movement(db, "Row")
            rows = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (ex_id,))
            assert rows[0]["name"] == "Row"
            assert rows[0]["section"] == "Cardio"
            assert rows[0]["metric"] == "time"
            assert rows[0]["ad_hoc"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_second_use_reuses_the_same_row(tmp_path):
    async def run():
        db = await _db(tmp_path)
        try:
            first = await resolve_movement(db, "Thruster")
            second = await resolve_movement(db, "Thruster")
            assert first == second
            rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises WHERE name = 'Thruster'")
            assert rows[0]["c"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_matches_existing_protocol_exercise_without_creating(tmp_path):
    async def run():
        db = await _db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, metric, builtin) VALUES ('Core', 'Goblet Squat', 'reps', 1)"
            )
            ex_id = await resolve_movement(db, "goblet squat")
            rows = await db.execute_fetchall("SELECT ad_hoc FROM training_exercises WHERE id = ?", (ex_id,))
            assert rows[0]["ad_hoc"] == 0, "an existing protocol exercise must not be re-created as ad hoc"
            count = await db.execute_fetchall(
                "SELECT COUNT(*) as c FROM training_exercises WHERE name = 'Goblet Squat'"
            )
            assert count[0]["c"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_archived_training_exercise_is_reactivated_by_a_new_wod(tmp_path, caplog):
    """M1 (2026-07-30 review): the training_exercises lookup used to match
    regardless of `archived`, silently reattaching new WOD entries to a
    retired row that still feeds Volume/PBs (neither query filters
    `archived`) while staying hidden from the protocol form. Reusing it is
    correct — a fresh WOD means it's not retired anymore — but leaving it
    archived while it accumulates history is the incoherent half of the old
    behaviour; the fix un-archives the row it reuses, with a log line."""

    async def run():
        db = await _db(tmp_path)
        try:
            cur = await db.execute(
                "INSERT INTO training_exercises (name, section, archived) VALUES ('Wall Ball', 'Core', 1)"
            )
            await db.commit()
            archived_id = cur.lastrowid

            with caplog.at_level("INFO"):
                ex_id = await resolve_movement(db, "wall ball")

            assert ex_id == archived_id, "must reuse the existing row, not create a second one"
            rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises WHERE name = 'Wall Ball'")
            assert rows[0]["c"] == 1, "must not create a duplicate row alongside the archived one"
            row = await db.execute_fetchall("SELECT archived FROM training_exercises WHERE id = ?", (archived_id,))
            assert row[0]["archived"] == 0, "a WOD naming a retired movement must reactivate it"
            assert any("wall ball" in r.message.lower() and "reactivat" in r.message.lower() for r in caplog.records), (
                "reactivation must be logged"
            )
        finally:
            await db.close()

    asyncio.run(run())


def test_non_archived_training_exercise_is_reused_without_extra_writes(tmp_path, caplog):
    """Control for M1: a normal (non-archived) existing row must still be
    matched and returned untouched — the reactivation UPDATE/log must only
    fire when `archived` was actually set, not unconditionally on every
    match (checking the end state alone can't tell "stayed 0" apart from
    "was flipped 0 -> 0 anyway", so this also asserts on the log)."""

    async def run():
        db = await _db(tmp_path)
        try:
            with caplog.at_level("INFO"):
                ex_id = await resolve_movement(db, "goblet squat")
            row = await db.execute_fetchall("SELECT archived FROM training_exercises WHERE id = ?", (ex_id,))
            assert row[0]["archived"] == 0
            assert not any("reactivat" in r.message.lower() for r in caplog.records), (
                "a non-archived row must not trigger the reactivation path at all"
            )
        finally:
            await db.close()

    asyncio.run(run())


def test_unknown_movement_creates_nothing(tmp_path):
    async def run():
        db = await _db(tmp_path)
        try:
            before = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises")
            assert await resolve_movement(db, "Devil Press") is None
            after = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_exercises")
            assert after[0]["c"] == before[0]["c"]
        finally:
            await db.close()

    asyncio.run(run())


def test_non_crossfit_library_row_is_resolvable(tmp_path):
    """The category filter used to gate this lookup the same way it gated
    canonical_movements()'s vocabulary — a Warmup/Stretching/Gym-classics/
    Kettlebell row could never be created via a WOD even once the parser was
    allowed to name it. Both must agree, or the parser proposes a movement
    that then silently fails to resolve. (`category` itself is gone since
    migration 019 — this now exercises an untagged Warmup row instead.)"""

    async def run():
        db = await _db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, metric, builtin) "
                "VALUES ('Warmup', 'Band Pull-apart', 'reps', 0)"
            )
            await db.commit()
            ex_id = await resolve_movement(db, "Band Pull-apart")
            assert ex_id is not None, "a non-CrossFit library row must now resolve"
            rows = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (ex_id,))
            assert rows[0]["section"] == "Warmup"
            assert rows[0]["metric"] == "reps"
            assert rows[0]["ad_hoc"] == 1
        finally:
            await db.close()

    asyncio.run(run())


def test_non_ascii_duplicate_names_each_resolve_unambiguously(tmp_path):
    """resolve_movement() used to break ties with `ORDER BY display_order
    LIMIT 1`, "just in case" two rows matched. They can't: exercise_library's
    UNIQUE(name COLLATE NOCASE) is ASCII-only, so 'Ćwiczenie'/'ćwiczenie' (a
    non-ASCII case pair) can coexist in the table (proven below), but the
    lookup here — `WHERE lower(name) = lower(?)` — compares both sides with
    that SAME SQL lower(), so if two rows both matched one `clean` value they
    would, by transitivity, also match each other and would already have
    violated the UNIQUE constraint. This inserts both spellings, with
    DIFFERENT section/metric so a wrong pick is visible, and resolves each
    exact name — proving neither query is ever ambiguous, which is why the
    ORDER BY/LIMIT tie-break was removed rather than kept as a no-op."""

    async def run():
        db = await _db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, metric, builtin) VALUES ('Core', 'Ćwiczenie', 'reps', 0)"
            )
            await db.execute(
                "INSERT INTO exercise_library (section, name, metric, builtin) "
                "VALUES ('Cardio', 'ćwiczenie', 'time', 0)"
            )
            await db.commit()

            rows = await db.execute_fetchall(
                "SELECT name FROM exercise_library WHERE name IN ('Ćwiczenie', 'ćwiczenie')"
            )
            assert len(rows) == 2, "both non-ASCII case-variant rows must have been accepted by the UNIQUE constraint"

            upper_id = await resolve_movement(db, "Ćwiczenie")
            lower_id = await resolve_movement(db, "ćwiczenie")
            assert upper_id != lower_id, "each spelling must resolve to its own row, not collide"

            upper_row = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (upper_id,))
            lower_row = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (lower_id,))
            assert upper_row[0]["section"] == "Core"
            assert upper_row[0]["metric"] == "reps"
            assert lower_row[0]["section"] == "Cardio"
            assert lower_row[0]["metric"] == "time"
        finally:
            await db.close()

    asyncio.run(run())


def test_duplicate_library_name_is_rejected_by_unique_constraint(tmp_path):
    """Before migration 019, exercise_library was UNIQUE(category, name), so
    'Back Squat' could exist twice (Gym classics + CrossFit) and
    resolve_movement() needed an explicit CrossFit-preferring tie-break to
    avoid resolving to the wrong section/metric — exactly the class of defect
    already fixed in training.py's picker path (B3). UNIQUE is now (name
    COLLATE NOCASE), so the duplicate itself can no longer be written; this
    proves the constraint holds, and that a movement seeded before the
    (rejected) second insert still resolves to its own section/metric
    untouched. The second insert deliberately differs only by case ('back
    squat' vs 'Back Squat') — a binary UNIQUE(name), with no COLLATE, would
    let this one through and this test would pass for the wrong reason."""

    async def run():
        db = await _db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric, builtin) "
                "VALUES ('Warmup', 'Back Squat', 1, 'time', 0)"
            )
            await db.commit()
            try:
                await db.execute(
                    "INSERT INTO exercise_library (section, name, display_order, metric, builtin) "
                    "VALUES ('Core', 'back squat', 50, 'reps', 0)"
                )
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            assert raised, "a case-variant duplicate name must be rejected by UNIQUE(name COLLATE NOCASE)"

            ex_id = await resolve_movement(db, "Back Squat")
            rows = await db.execute_fetchall("SELECT * FROM training_exercises WHERE id = ?", (ex_id,))
            assert rows[0]["section"] == "Warmup"
            assert rows[0]["metric"] == "time"
        finally:
            await db.close()

    asyncio.run(run())
