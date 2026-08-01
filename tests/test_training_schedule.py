"""The weekly schedule block fed to the A.N.D.Y. planner.

This block replaced a prescription list read from `training_exercises`. The
value it carries goes straight into an LLM prompt, so the tests here care about
two things: that a malformed setting degrades instead of destroying the block,
and that the "what got logged" half is bounded by the date being planned.
"""

import asyncio
import sqlite3
from datetime import date

import aiosqlite
import pytest

from app.services.training_schedule import (
    DEFAULT_DAYS,
    SETTING_DAYS,
    SETTING_SWIM,
    format_days,
    normalize_days,
    parse_swim_per_week,
    schedule_block,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("mon,wed,fri", ["mon", "wed", "fri"]),
        # Canonical weekday order, not input order.
        ("fri,mon", ["mon", "fri"]),
        ("Monday, WEDNESDAY , fri", ["mon", "wed", "fri"]),
        ("mon,mon,mon", ["mon"]),
        ("", []),
        (",,,", []),
        # Unknown tokens are dropped, the rest survives — one typo must not
        # cost the planner the whole schedule.
        ("mon,xyz,fri", ["mon", "fri"]),
        ("xyz", []),
    ],
)
def test_normalize_days(raw, expected):
    assert normalize_days(raw) == expected


def test_normalize_days_covers_every_weekday():
    """Guards the [:3] prefix trick against a day whose abbreviation it breaks."""
    full = "monday,tuesday,wednesday,thursday,friday,saturday,sunday"
    assert normalize_days(full) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("0", 0), ("", 0), ("abc", 0), ("-3", 0), ("99", 7), (" 2 ", 2)],
)
def test_parse_swim_per_week(raw, expected):
    assert parse_swim_per_week(raw) == expected


def test_format_days():
    assert format_days(["mon", "wed", "fri"]) == "Mon, Wed, Fri"


def _make_db(tmp_path):
    path = tmp_path / "sched.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        CREATE TABLE training_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            duration_minutes INTEGER
        );
        """
    )
    conn.commit()
    conn.close()
    return path


def _block(path, target, settings=None, sessions=()):
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM app_settings")
        conn.execute("DELETE FROM training_sessions")
        for key, value in (settings or {}).items():
            conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        for session_date, duration in sessions:
            conn.execute(
                "INSERT INTO training_sessions (date, duration_minutes) VALUES (?, ?)", (session_date, duration)
            )
        conn.commit()
    finally:
        conn.close()

    async def run():
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        try:
            return await schedule_block(db, target)
        finally:
            await db.close()

    return asyncio.run(run())


def test_scheduled_day_is_named_as_such(tmp_path):
    path = _make_db(tmp_path)
    # 2026-08-03 is a Monday.
    block = _block(path, date(2026, 8, 3), {SETTING_DAYS: "mon,wed,fri", SETTING_SWIM: "1"})
    assert "CrossFit days: Mon, Wed, Fri." in block
    assert "Swimming: 1x/week, any day." in block
    assert "Today is Monday — a scheduled CrossFit day." in block


def test_unscheduled_day_is_named_as_such(tmp_path):
    path = _make_db(tmp_path)
    # 2026-08-04 is a Tuesday, absent from mon,wed,fri.
    block = _block(path, date(2026, 8, 4), {SETTING_DAYS: "mon,wed,fri"})
    assert "Today is Tuesday — not a scheduled CrossFit day." in block


def test_zero_swim_target_drops_the_swim_sentence(tmp_path):
    path = _make_db(tmp_path)
    block = _block(path, date(2026, 8, 3), {SETTING_DAYS: "mon", SETTING_SWIM: "0"})
    assert "Swimming" not in block
    assert "CrossFit days: Mon." in block, "the rest of the plan must survive"


def test_no_days_configured_still_produces_a_usable_block(tmp_path):
    path = _make_db(tmp_path)
    block = _block(path, date(2026, 8, 3), {SETTING_DAYS: "", SETTING_SWIM: "1"})
    assert "No fixed CrossFit days set." in block
    assert "Today is Monday — not a scheduled CrossFit day." in block


def test_defaults_apply_when_the_settings_row_is_absent(tmp_path):
    """A fresh DB (or one predating the seed) must not yield an empty plan."""
    path = _make_db(tmp_path)
    block = _block(path, date(2026, 8, 3), {})
    assert format_days(normalize_days(DEFAULT_DAYS)) in block


def test_logged_sessions_are_listed_for_the_current_week(tmp_path):
    path = _make_db(tmp_path)
    block = _block(
        path,
        date(2026, 8, 5),  # Wednesday
        {SETTING_DAYS: "mon,wed,fri"},
        sessions=[("2026-08-03", 55), ("2026-08-04", None)],
    )
    assert "Logged this week (since Monday): 2026-08-03 (55 min), 2026-08-04." in block


def test_sessions_after_the_planned_date_are_excluded(tmp_path):
    """The planner can be run for a past date; that day cannot know about a
    session logged later in the same week."""
    path = _make_db(tmp_path)
    block = _block(
        path,
        date(2026, 8, 3),  # Monday
        {SETTING_DAYS: "mon"},
        sessions=[("2026-08-03", 40), ("2026-08-05", 60)],
    )
    assert "2026-08-03 (40 min)" in block
    assert "2026-08-05" not in block, "a later session must not leak into an earlier day's plan"


def test_sessions_before_this_week_are_excluded_from_the_week_list(tmp_path):
    path = _make_db(tmp_path)
    block = _block(
        path,
        date(2026, 8, 5),
        {SETTING_DAYS: "mon,wed"},
        sessions=[("2026-07-31", 45), ("2026-08-05", 50)],
    )
    assert "Logged this week (since Monday): 2026-08-05 (50 min)." in block
    assert "2026-07-31" not in block


def test_empty_week_falls_back_to_the_previous_session(tmp_path):
    path = _make_db(tmp_path)
    block = _block(
        path,
        date(2026, 8, 5),
        {SETTING_DAYS: "mon,wed"},
        sessions=[("2026-07-31", 45)],
    )
    assert "Logged this week (since Monday): nothing yet." in block
    assert "Last session before this week: 2026-07-31 (45 min)." in block


def test_empty_week_and_no_history_omits_the_fallback_line(tmp_path):
    path = _make_db(tmp_path)
    block = _block(path, date(2026, 8, 5), {SETTING_DAYS: "mon,wed"})
    assert "Logged this week (since Monday): nothing yet." in block
    assert "Last session before this week" not in block


# --- Alignment and boundary coverage (added after review: three mutants survived
#     the first pass — the Monday cut-off, and DAY_FULL/DAY_SHORT's weekend slots) ---


@pytest.mark.parametrize(
    ("target", "expected_full"),
    [
        (date(2026, 8, 3), "Monday"),
        (date(2026, 8, 4), "Tuesday"),
        (date(2026, 8, 5), "Wednesday"),
        (date(2026, 8, 6), "Thursday"),
        (date(2026, 8, 7), "Friday"),
        (date(2026, 8, 8), "Saturday"),
        (date(2026, 8, 9), "Sunday"),
    ],
)
def test_every_weekday_is_named_correctly(tmp_path, target, expected_full):
    """DAY_FULL must stay aligned with date.weekday().

    The first test pass only ever rendered Mondays and Tuesdays, so swapping
    Saturday and Sunday in DAY_FULL left the whole suite green — and this
    sentence is what tells the planner whether to program a session.
    """
    path = _make_db(tmp_path)
    block = _block(path, target, {SETTING_DAYS: "mon"})
    assert f"Today is {expected_full} —" in block


@pytest.mark.parametrize(
    ("day_key", "expected_short"),
    [
        ("mon", "Mon"),
        ("tue", "Tue"),
        ("wed", "Wed"),
        ("thu", "Thu"),
        ("fri", "Fri"),
        ("sat", "Sat"),
        ("sun", "Sun"),
    ],
)
def test_every_day_abbreviation_is_aligned(day_key, expected_short):
    """Same gap as above, for DAY_SHORT: only Mon/Wed/Fri were ever formatted."""
    assert format_days([day_key]) == expected_short


def test_week_starts_on_monday_at_the_boundary(tmp_path):
    """The Sunday immediately before the target week must be excluded.

    The existing out-of-week fixture was a Friday — three days clear of the cut —
    so shifting the week start by one day (`weekday + 1`) changed nothing and the
    suite stayed green. 2026-08-02 is the Sunday before, 2026-08-03 the Monday.
    """
    path = _make_db(tmp_path)
    block = _block(
        path,
        date(2026, 8, 5),  # Wednesday
        {SETTING_DAYS: "mon,wed"},
        sessions=[("2026-08-02", 30), ("2026-08-03", 55)],
    )
    assert "Logged this week (since Monday): 2026-08-03 (55 min)." in block
    assert "2026-08-02" not in block, "the Sunday before the week must fall outside it"


def test_blank_day_list_clears_the_schedule(tmp_path):
    """Pins the deliberate asymmetry in save_training_schedule: a blank field is
    an explicit "no fixed days" and is stored; a field that parses to nothing is
    rejected. Both semantics were green before this test existed."""
    path = _make_db(tmp_path)
    block = _block(path, date(2026, 8, 3), {SETTING_DAYS: ""})
    assert "No fixed CrossFit days set." in block
