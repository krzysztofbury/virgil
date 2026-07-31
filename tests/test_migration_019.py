"""Migration 019: category -> tags, UNIQUE(name), duplicate merge.

The production database is not knowable from this repo — the user has been
adding library rows through Settings and the REST API since deployment — so
these fixtures cover arbitrary categories and arbitrary duplicates, not only
the seven categories and four duplicate names that ship in the seed data.
"""

import asyncio
import importlib

import aiosqlite


async def _legacy_db(tmp_path, rows):
    db = await aiosqlite.connect(tmp_path / "legacy.db")
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
    for r in rows:
        await db.execute(
            "INSERT INTO exercise_library "
            "(category, section, name, sets, reps, notes, display_order, metric, builtin, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["category"],
                r["section"],
                r["name"],
                r.get("sets"),
                r.get("reps", ""),
                r.get("notes", ""),
                r.get("display_order", 0),
                r.get("metric", "reps"),
                r.get("builtin", 1),
                r.get("archived", 0),
            ),
        )
    await db.commit()
    return db


def _run(coro_factory):
    return asyncio.run(coro_factory())


async def _tags_of(db, name):
    rows = await db.execute_fetchall(
        "SELECT t.tag FROM exercise_library_tags t "
        "JOIN exercise_library l ON l.id = t.library_id WHERE l.name = ? ORDER BY t.tag",
        (name,),
    )
    return [r["tag"] for r in rows]


def test_style_categories_become_tags(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster"},
                {"category": "Kettlebell", "section": "Core", "name": "KB Swing"},
                {"category": "Gym classics", "section": "Core", "name": "Leg Press"},
                {"category": "Bodyweight", "section": "Core", "name": "Air Squat"},
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        assert await _tags_of(db, "Thruster") == ["crossfit"]
        assert await _tags_of(db, "KB Swing") == ["kettlebell"]
        assert await _tags_of(db, "Leg Press") == ["gym-classic"]
        assert await _tags_of(db, "Air Squat") == ["bodyweight"]
        await db.close()

    _run(lambda: run())


def test_section_echo_and_program_categories_produce_no_tag(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "Warmup", "section": "Warmup", "name": "Jumping Jacks"},
                {"category": "Stretching", "section": "Stretching", "name": "Couch Stretch"},
                {"category": "Cardio", "section": "Cardio", "name": "Jump Rope"},
                {"category": "Workout A (KB full-body)", "section": "Core", "name": "Goblet Squat"},
                {"category": "Workout B (KB full-body)", "section": "Core", "name": "Floor Press"},
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        for name in ("Jumping Jacks", "Couch Stretch", "Jump Rope", "Goblet Squat", "Floor Press"):
            assert await _tags_of(db, name) == [], name
        rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM exercise_library")
        assert rows[0]["c"] == 5, "rows must survive even though they get no tag"
        await db.close()

    _run(lambda: run())


def test_unknown_user_category_is_normalised(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "My HYROX prep!!", "section": "Cardio", "name": "Sled Push"},
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        assert await _tags_of(db, "Sled Push") == ["my-hyrox-prep"]
        await db.close()

    _run(lambda: run())


def test_duplicate_names_merge_keeping_the_explicitly_seeded_metric(tmp_path):
    async def run():
        # 'Row' is in CROSSFIT_MOVEMENTS as Cardio/time. The legacy row has the
        # metric migration 011 would have DERIVED, and a lower display_order --
        # so "lowest display_order wins" would silently mis-type it.
        db = await _legacy_db(
            tmp_path,
            [
                {
                    "category": "Cardio",
                    "section": "Cardio",
                    "name": "Row",
                    "metric": "reps",
                    "display_order": 5,
                    "builtin": 1,
                },
                {
                    "category": "CrossFit",
                    "section": "Cardio",
                    "name": "Row",
                    "metric": "time",
                    "display_order": 60,
                    "builtin": 0,
                },
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Row'")
        assert len(rows) == 1
        assert rows[0]["metric"] == "time", "the explicitly seeded metric must survive the merge"
        assert rows[0]["builtin"] == 0, "editability must not be taken away by merging"
        assert await _tags_of(db, "Row") == ["crossfit"]
        await db.close()

    _run(lambda: run())


def test_merge_unions_tags_and_keeps_the_active_state(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {
                    "category": "Gym classics",
                    "section": "Core",
                    "name": "Back Squat",
                    "display_order": 3,
                    "builtin": 1,
                    "archived": 1,
                },
                {
                    "category": "CrossFit",
                    "section": "Core",
                    "name": "Back Squat",
                    "display_order": 50,
                    "builtin": 0,
                    "archived": 0,
                },
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Back Squat'")
        assert len(rows) == 1
        assert rows[0]["archived"] == 0, "a movement visible today must not vanish"
        assert await _tags_of(db, "Back Squat") == ["crossfit", "gym-classic"]
        await db.close()

    _run(lambda: run())


def test_user_row_colliding_with_a_seeded_name_merges(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Burpee", "display_order": 55, "builtin": 0},
                {
                    "category": "Moje",
                    "section": "Core",
                    "name": "Burpee",
                    "display_order": 90,
                    "builtin": 0,
                    "notes": "moja wersja",
                },
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Burpee'")
        assert len(rows) == 1
        assert await _tags_of(db, "Burpee") == ["crossfit", "moje"]
        await db.close()

    _run(lambda: run())


def test_category_column_is_gone_and_name_is_unique(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster"},
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
        assert "category" not in {c["name"] for c in cols}
        idx = await db.execute_fetchall("PRAGMA index_list(exercise_library)")
        uniques = [i for i in idx if i["unique"]]
        assert uniques, "UNIQUE(name) must exist"
        await db.close()

    _run(lambda: run())


def test_is_idempotent(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster"},
            ],
        )
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM exercise_library")
        assert rows[0]["c"] == 1
        assert await _tags_of(db, "Thruster") == ["crossfit"]
        await db.close()

    _run(lambda: run())


def test_training_history_is_untouched(tmp_path):
    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster"},
            ],
        )
        await db.execute(
            """CREATE TABLE training_exercises (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, section TEXT NOT NULL,
                metric TEXT NOT NULL DEFAULT 'reps', ad_hoc INTEGER NOT NULL DEFAULT 0)"""
        )
        await db.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('Thruster','Core','reps',1)"
        )
        await db.commit()
        mod = importlib.import_module("app.migrations.019_exercise_tags")
        await mod.up(db)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM training_exercises")
        assert len(rows) == 1 and rows[0]["name"] == "Thruster" and rows[0]["ad_hoc"] == 1
        await db.close()

    _run(lambda: run())
