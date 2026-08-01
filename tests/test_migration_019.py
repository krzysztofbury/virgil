"""Migration 019: category -> tags, UNIQUE(name), duplicate merge.

The production database is not knowable from this repo — the user has been
adding library rows through Settings and the REST API since deployment — so
these fixtures cover arbitrary categories and arbitrary duplicates, not only
the seven categories and four duplicate names that ship in the seed data.
"""

import asyncio
import importlib

import aiosqlite

from app.library_validation import MAX_TAG_LEN


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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "Thruster") == ["crossfit"]
            assert await _tags_of(db, "KB Swing") == ["kettlebell"]
            assert await _tags_of(db, "Leg Press") == ["gym-classic"]
            assert await _tags_of(db, "Air Squat") == ["bodyweight"]
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            for name in ("Jumping Jacks", "Couch Stretch", "Jump Rope", "Goblet Squat", "Floor Press"):
                assert await _tags_of(db, name) == [], name
            rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM exercise_library")
            assert rows[0]["c"] == 5, "rows must survive even though they get no tag"
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "Sled Push") == ["my-hyrox-prep"]
        finally:
            await db.close()

    _run(lambda: run())


def test_non_ascii_name_keeps_its_tag(tmp_path):
    """SQLite's lower() is ASCII-only, while the grouping key is built with
    Python's Unicode-aware str.lower() -- a name like 'ĆWICZENIE' becomes
    'ćwiczenie' in Python but SQLite's lower('ĆWICZENIE') leaves the accented
    letters untouched. A tag-attachment step that looked the survivor back up
    by `lower(name) = key` would silently miss and drop the tag entirely;
    keying off the INSERT's own lastrowid instead has no such gap."""

    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "ĆWICZENIE"},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "ĆWICZENIE") == ["crossfit"]
        finally:
            await db.close()

    _run(lambda: run())


def test_long_category_is_truncated_not_dropped(tmp_path):
    """Categories were free text capped at 100 characters on both write
    surfaces (settings.py and api.py) -- well past MAX_TAG_LEN (40). A
    category this long is real user data, not noise, so the tag it produces
    must be truncated, not silently dropped the way a pure-punctuation
    category is."""

    async def run():
        long_category = "Y" * 60
        db = await _legacy_db(
            tmp_path,
            [
                {"category": long_category, "section": "Cardio", "name": "Sandbag Carry"},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "Sandbag Carry") == ["y" * MAX_TAG_LEN]
        finally:
            await db.close()

    _run(lambda: run())


def test_long_category_with_polish_letters_is_transliterated_and_truncated(tmp_path):
    """Polish letters (here "ł", which has no NFKD decomposition and folds
    1:1 via the explicit transliteration map) don't change length, so a
    60-char category built entirely of them must still truncate to a
    40-char ASCII tag -- same guarantee as the plain-ASCII long-category
    case, now going through the transliteration step first."""

    async def run():
        long_category = "Ł" * 60
        db = await _legacy_db(
            tmp_path,
            [
                {"category": long_category, "section": "Cardio", "name": "Sandbag Carry"},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "Sandbag Carry") == ["l" * MAX_TAG_LEN]
        finally:
            await db.close()

    _run(lambda: run())


def test_category_that_still_overflows_after_transliteration_is_dropped_not_raised(tmp_path):
    """Unlike the old character filter, transliteration can LENGTHEN a
    string (ß -> "ss"): 40 raw "ß" become 80 ascii "s", which still exceeds
    MAX_TAG_LEN even after truncating the raw category to 40 chars first.
    The migration must not blow up over this -- the row survives with no
    tag, same as a pure-punctuation category."""

    async def run():
        long_category = "ß" * 60
        db = await _legacy_db(
            tmp_path,
            [
                {"category": long_category, "section": "Cardio", "name": "Farmer Carry"},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            assert await _tags_of(db, "Farmer Carry") == []
            rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM exercise_library")
            assert rows[0]["c"] == 1, "the row must survive even though it gets no tag"
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Row'")
            assert len(rows) == 1
            assert rows[0]["metric"] == "time", "the explicitly seeded metric must survive the merge"
            assert rows[0]["builtin"] == 0, "editability must not be taken away by merging"
            assert await _tags_of(db, "Row") == ["crossfit"]
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Back Squat'")
            assert len(rows) == 1
            assert rows[0]["archived"] == 0, "a movement visible today must not vanish"
            assert await _tags_of(db, "Back Squat") == ["crossfit", "gym-classic"]
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE name = 'Burpee'")
            assert len(rows) == 1
            assert rows[0]["notes"] == "moja wersja", "notes must be backfilled from the sibling that has them"
            assert await _tags_of(db, "Burpee") == ["crossfit", "moje"]
        finally:
            await db.close()

    _run(lambda: run())


def test_duplicate_name_with_whitespace_still_gets_tagged(tmp_path):
    """019 groups rows by `name.strip().lower()` but the survivor's raw name
    kept its whitespace, so the tag-attachment lookup (by `lower(name)`)
    would miss the padded row entirely and silently drop its tags."""

    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster "},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT * FROM exercise_library")
            assert len(rows) == 1
            assert rows[0]["name"] == "Thruster", "the stored name must be stripped"
            assert await _tags_of(db, "Thruster") == ["crossfit"]
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
            assert "category" not in {c["name"] for c in cols}
            idx = await db.execute_fetchall("PRAGMA index_list(exercise_library)")
            uniques = [i for i in idx if i["unique"]]
            assert uniques, "UNIQUE(name) must exist"
            info = await db.execute_fetchall(f"PRAGMA index_info({uniques[0]['name']})")
            assert [c["name"] for c in info] == ["name"], "the unique index must be exactly on name, nothing else"
        finally:
            await db.close()

    _run(lambda: run())


def test_case_variant_duplicate_is_rejected_after_migration(tmp_path):
    """UNIQUE(name) is binary-collated by default -- 'Thruster' and 'thruster'
    would otherwise both be insertable, silently splitting the WOD parser's
    vocabulary (which dedupes by lower(name)) between two rows for the same
    movement. The rebuilt table declares UNIQUE(name COLLATE NOCASE)
    specifically to close that."""

    async def run():
        db = await _legacy_db(
            tmp_path,
            [
                {"category": "CrossFit", "section": "Core", "name": "Thruster"},
            ],
        )
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await db.commit()
            try:
                await db.execute(
                    "INSERT INTO exercise_library (section, name, metric, builtin) "
                    "VALUES ('Core', 'thruster', 'reps', 0)"
                )
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            assert raised, "a case-variant duplicate name must be rejected by UNIQUE(name COLLATE NOCASE)"
        finally:
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
        try:
            mod = importlib.import_module("app.migrations.019_exercise_tags")
            await mod.up(db)
            await mod.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT COUNT(*) AS c FROM exercise_library")
            assert rows[0]["c"] == 1
            assert await _tags_of(db, "Thruster") == ["crossfit"]
        finally:
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
        try:
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
        finally:
            await db.close()

    _run(lambda: run())
