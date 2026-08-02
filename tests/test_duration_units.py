"""training_entries.duration is seconds — the formatter and the one-time repair.

The column carried two units: the deleted per-set log form wrote minutes for
Warmup/Cardio/Stretching and seconds only for Core+time, while confirm_wod (now
the only writer) always writes seconds. The page printed the raw value with a
literal " min", so a 69-minute ride stored as 4140 read "4140.0 min" beside a
header that said "69 min".
"""

import asyncio
import importlib
from datetime import date

import aiosqlite
import pytest

from app.formatting import format_duration_seconds

_MIGRATION = importlib.import_module("app.migrations.020_duration_seconds")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (45, "45 s"),
        (59, "59 s"),
        (60, "1 min"),
        (90, "1 min 30 s"),
        (3000, "50 min"),
        # The value from the screenshot that surfaced this, beside a header
        # reading "69 min" — the two must now agree.
        (4140, "69 min"),
        (4140.0, "69 min"),
    ],
)
def test_format_duration_seconds(seconds, expected):
    assert format_duration_seconds(seconds) == expected


@pytest.mark.parametrize("value", [None, "", "abc", 0, -5, [], {}])
def test_format_duration_returns_blank_for_unusable_values(value):
    """Blank, not "None" and not a crash — the caller supplies its own dash."""
    assert format_duration_seconds(value) == ""


def test_format_never_prints_minutes_for_a_sub_minute_value():
    """The defect in one line: a 45-second plank must not read as 45 minutes."""
    assert "min" not in format_duration_seconds(45)


# --- Migration 020 -------------------------------------------------------


async def _legacy_db(tmp_path, entries):
    """A database shaped like the deployed one: both writers' rows side by side.

    `entries` are (entry_id, section, metric, session_date, duration) tuples.
    """
    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(
        """
        CREATE TABLE training_exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            section TEXT NOT NULL,
            metric TEXT NOT NULL DEFAULT 'reps'
        );
        CREATE TABLE training_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL
        );
        CREATE TABLE training_entries (
            id INTEGER PRIMARY KEY,
            session_id INTEGER NOT NULL REFERENCES training_sessions(id),
            exercise_id INTEGER NOT NULL REFERENCES training_exercises(id),
            set_number INTEGER NOT NULL DEFAULT 1,
            duration REAL
        );
        """
    )
    for entry_id, section, metric, session_date, duration in entries:
        cur = await db.execute(
            "INSERT INTO training_exercises (name, section, metric) VALUES (?, ?, ?)",
            (f"Move {entry_id}", section, metric),
        )
        ex_id = cur.lastrowid
        cur = await db.execute("INSERT INTO training_sessions (date) VALUES (?)", (session_date,))
        await db.execute(
            "INSERT INTO training_entries (id, session_id, exercise_id, duration) VALUES (?, ?, ?, ?)",
            (entry_id, cur.lastrowid, ex_id, duration),
        )
    await db.commit()
    return db


def _run(tmp_path, entries):
    async def go():
        db = await _legacy_db(tmp_path, entries)
        try:
            await _MIGRATION.up(db)
            await db.commit()
            rows = await db.execute_fetchall("SELECT id, duration FROM training_entries ORDER BY id")
            return {r["id"]: r["duration"] for r in rows}
        finally:
            await db.close()

    return asyncio.run(go())


# The deployed database's exact shape on 2026-08-02, one row per distinct case.
_REAL_SHAPE = [
    (18, "Warmup", "time", "2026-07-06", 1.0),  # jump rope, 1 min
    (19, "Warmup", "reps", "2026-07-06", 15.0),  # halo, 15 min
    (55, "Core", "time", "2026-07-14", 60.0),  # farmer's walk, 60 s
    (57, "Core", "time", "2026-07-14", 45.0),  # plank, 45 s
    (81, "Cardio", "reps", "2026-07-16", 50.0),  # swim, 50 min
    (104, "Cardio", "time", "2026-07-28", 60.0),  # WOD run, 60 s
    (103, "Cardio", "reps", "2026-07-30", 50.0),  # swim, 50 min
    (121, "Cardio", "time", "2026-08-01", 4140.0),  # WOD bike, 4140 s
]


def test_converts_exactly_the_legacy_minute_rows(tmp_path):
    after = _run(tmp_path, _REAL_SHAPE)
    assert after == {
        18: 60.0,  # 1 min -> 60 s
        19: 900.0,  # 15 min -> 900 s
        55: 60.0,  # already seconds
        57: 45.0,  # already seconds
        81: 3000.0,  # 50 min -> 3000 s
        104: 60.0,  # already seconds (WOD)
        103: 3000.0,  # 50 min -> 3000 s
        121: 4140.0,  # already seconds (WOD)
    }


def test_core_time_is_never_converted(tmp_path):
    """Core+metric='time' was the old writer's only seconds branch, and the new
    writer's rows are seconds too — so it is seconds regardless of origin. A
    45-second plank multiplied by 60 would become 45 minutes."""
    after = _run(tmp_path, [(1, "Core", "time", "2026-07-14", 45.0)])
    assert after == {1: 45.0}


def test_cardio_time_is_never_converted(tmp_path):
    """Only confirm_wod could write Cardio+time, and it writes seconds."""
    after = _run(tmp_path, [(1, "Cardio", "time", "2026-07-28", 60.0)])
    assert after == {1: 60.0}


def test_rows_past_the_verified_ceiling_are_left_alone(tmp_path):
    """ "Warmup is minutes" describes today's rows, not tomorrow's: a WOD note
    naming a warm-up movement writes a Warmup row in seconds. The id and date
    ceilings bound the repair to the rows actually verified, so anything captured
    between that verification and this migration running is untouched."""
    after = _run(
        tmp_path,
        [
            (103, "Warmup", "time", "2026-07-20", 2.0),  # within both ceilings
            (200, "Warmup", "time", "2026-07-20", 2.0),  # id past the ceiling
            (99, "Warmup", "time", "2026-08-05", 2.0),  # date past the ceiling
        ],
    )
    assert after == {103: 120.0, 200: 2.0, 99: 2.0}


def test_null_and_zero_durations_are_untouched(tmp_path):
    after = _run(tmp_path, [(1, "Warmup", "time", "2026-07-06", None), (2, "Warmup", "time", "2026-07-06", 0.0)])
    assert after == {1: None, 2: 0.0}


def test_a_fresh_install_converts_nothing(tmp_path):
    assert _run(tmp_path, []) == {}


def test_pre_011_schema_without_metric_is_skipped(tmp_path):
    """A database predating the metric column cannot have the rule evaluated —
    and also predates every row this targets. It must not raise."""

    async def go():
        db = await aiosqlite.connect(tmp_path / "old.db")
        db.row_factory = aiosqlite.Row
        await db.executescript(
            """
            CREATE TABLE training_exercises (id INTEGER PRIMARY KEY, name TEXT, section TEXT);
            CREATE TABLE training_sessions (id INTEGER PRIMARY KEY, date TEXT);
            CREATE TABLE training_entries (id INTEGER PRIMARY KEY, session_id INTEGER,
                                           exercise_id INTEGER, duration REAL);
            """
        )
        await db.commit()
        try:
            await _MIGRATION.up(db)
        finally:
            await db.close()

    asyncio.run(go())


# --- End to end: the rendered page ---------------------------------------


def test_training_page_renders_seconds_as_readable_minutes(auth_client):
    """The reported defect, end to end: a 69-minute ride stored as 4140 rendered
    as "4140.0 min" in the row while the session header said "69 min".

    Both assertions matter. The absence check alone passes against a page that
    fails to render the entry at all.
    """
    import sqlite3

    from conftest import user_db_path

    conn = sqlite3.connect(user_db_path())
    ex_id = session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES (?, 'Cardio', 'time', 1)",
            ("ZZTestBikeDuration",),
        )
        ex_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 69, 'ZZ duration render')",
            (date.today().isoformat(),),
        )
        session_id = cur.lastrowid
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, duration) VALUES (?, ?, 1, 4140)",
            (session_id, ex_id),
        )
        conn.commit()

        html = auth_client.get("/training").text
        assert "ZZTestBikeDuration" in html, "precondition: the entry must render at all"
        assert "4140" not in html, "the raw second count must not reach the page"
        assert "69 min" in html, "4140 s must render as 69 min — the same figure the header shows"
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if ex_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (ex_id,))
        conn.commit()
        conn.close()
