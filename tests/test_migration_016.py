"""Migration 016: training_exercises.ad_hoc + CrossFit movement library.

ad_hoc marks movements created on demand by the WOD parser. They must stay out
of the protocol form but remain visible to history, volume and PBs.
"""

import asyncio
import importlib
from collections import Counter


async def _legacy_db(tmp_path):
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "legacy.db")
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
            archived INTEGER NOT NULL DEFAULT 0
        )"""
    )
    await db.execute(
        """CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category, name)
        )"""
    )
    await db.execute("INSERT INTO training_exercises (name, section) VALUES ('Goblet Squat', 'Core')")
    await db.commit()
    return db


def test_adds_ad_hoc_column_defaulting_to_zero(tmp_path):
    async def run():
        db = await _legacy_db(tmp_path)
        mod = importlib.import_module("app.migrations.016_crossfit_movements")
        await mod.up(db)
        await db.commit()
        cols = await db.execute_fetchall("PRAGMA table_info(training_exercises)")
        names = {c["name"] for c in cols}
        assert "ad_hoc" in names
        rows = await db.execute_fetchall("SELECT ad_hoc FROM training_exercises WHERE name = 'Goblet Squat'")
        assert rows[0]["ad_hoc"] == 0
        await db.close()

    asyncio.run(run())


def test_seeds_crossfit_movements_with_sections_and_metrics(tmp_path):
    async def run():
        db = await _legacy_db(tmp_path)
        mod = importlib.import_module("app.migrations.016_crossfit_movements")
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT name, section, metric, builtin FROM exercise_library WHERE category = 'CrossFit'"
        )
        by_name = {r["name"]: r for r in rows}
        assert len(rows) == 31, f"expected 31 CrossFit movements, got {len(rows)}"
        assert by_name["Thruster"]["section"] == "Core"
        assert by_name["Thruster"]["metric"] == "reps"
        assert by_name["Row"]["section"] == "Cardio"
        assert by_name["Row"]["metric"] == "time"
        assert by_name["Double-under"]["section"] == "Cardio"
        assert by_name["Double-under"]["metric"] == "reps"
        assert all(r["builtin"] == 0 for r in rows)
        # Verify the full vocabulary split
        assert Counter((r["section"], r["metric"]) for r in rows) == {
            ("Core", "reps"): 25,
            ("Cardio", "time"): 4,
            ("Cardio", "reps"): 2,
        }
        await db.close()

    asyncio.run(run())


def test_is_idempotent(tmp_path):
    async def run():
        db = await _legacy_db(tmp_path)
        mod = importlib.import_module("app.migrations.016_crossfit_movements")
        await mod.up(db)
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM exercise_library WHERE category = 'CrossFit'")
        assert rows[0]["c"] == 31
        await db.close()

    asyncio.run(run())


def test_crossfit_movements_stay_out_of_the_009_seed_list():
    """Migration 009 seeds EXERCISE_LIBRARY before the metric column exists, so a
    CrossFit-category row in that list would be mis-typed as metric='reps' forever.

    The invariant is on CATEGORY, not name. Name overlap is expected and harmless:
    exercise_library is UNIQUE(category, name), and 'Back Squat', 'Deadlift',
    'Bench Press' and 'Pull-up' already exist under 'Gym classics' / 'Workout B'.
    It is also desirable — a movement the user already trains must resolve to their
    existing exercise row rather than a duplicate (see Task 3,
    test_matches_existing_protocol_exercise_without_creating).
    """
    from app.exercise_library import CROSSFIT_MOVEMENTS, EXERCISE_LIBRARY

    assert not [e for e in EXERCISE_LIBRARY if e["category"] == "CrossFit"]
    assert len(CROSSFIT_MOVEMENTS) == 31
    assert all(m["category"] == "CrossFit" for m in CROSSFIT_MOVEMENTS)


def test_017_flips_crossfit_rows_to_user_editable(tmp_path):
    """Migration 017 corrects the builtin flag for CrossFit rows on already-migrated
    databases. This test simulates a DB that ran the old 016 (with builtin=1) and
    now runs 017.

    The critical assertion is that non-CrossFit rows stay builtin=1 — this catches
    a WHERE-clause typo that would flip everything.
    """

    async def run():
        import aiosqlite

        db = await aiosqlite.connect(tmp_path / "post_016.db")
        db.row_factory = aiosqlite.Row
        await db.execute(
            """CREATE TABLE exercise_library (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                section TEXT NOT NULL,
                name TEXT NOT NULL,
                sets INTEGER,
                reps TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                display_order INTEGER DEFAULT 0,
                metric TEXT NOT NULL DEFAULT 'reps',
                builtin INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                UNIQUE(category, name)
            )"""
        )
        # Simulate the old 016: CrossFit rows with builtin=1
        await db.execute(
            "INSERT INTO exercise_library (category, section, name, metric, builtin) VALUES (?, ?, ?, ?, 1)",
            ("CrossFit", "Core", "Thruster", "reps"),
        )
        # Simulate legacy rows from migration 015: non-CrossFit with builtin=1
        await db.execute(
            "INSERT INTO exercise_library (category, section, name, metric, builtin) VALUES (?, ?, ?, ?, 1)",
            ("Gym classics", "Core", "Back Squat", "reps"),
        )
        await db.commit()

        # Verify initial state
        rows_before = await db.execute_fetchall("SELECT category, name, builtin FROM exercise_library ORDER BY name")
        by_cat_name = {(r["category"], r["name"]): r["builtin"] for r in rows_before}
        assert by_cat_name[("CrossFit", "Thruster")] == 1, "CrossFit row starts at builtin=1"
        assert by_cat_name[("Gym classics", "Back Squat")] == 1, "Non-CrossFit row is builtin=1"

        # Run 017
        mod = importlib.import_module("app.migrations.017_crossfit_editable")
        await mod.up(db)
        await db.commit()

        # Verify 017 changed only the CrossFit row
        rows_after = await db.execute_fetchall("SELECT category, name, builtin FROM exercise_library ORDER BY name")
        by_cat_name = {(r["category"], r["name"]): r["builtin"] for r in rows_after}
        assert by_cat_name[("CrossFit", "Thruster")] == 0, "CrossFit row flipped to builtin=0"
        assert by_cat_name[("Gym classics", "Back Squat")] == 1, (
            "Non-CrossFit row still builtin=1 (catches WHERE-clause typo)"
        )

        # Run 017 again and verify idempotency
        await mod.up(db)
        await db.commit()

        rows_final = await db.execute_fetchall("SELECT category, name, builtin FROM exercise_library ORDER BY name")
        by_cat_name = {(r["category"], r["name"]): r["builtin"] for r in rows_final}
        assert by_cat_name[("CrossFit", "Thruster")] == 0, "Second run unchanged"
        assert by_cat_name[("Gym classics", "Back Squat")] == 1, "Non-CrossFit row still protected"

        await db.close()

    asyncio.run(run())
