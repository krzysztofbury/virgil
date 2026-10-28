"""Edging (near_miss) is its own question, separate from porn (slipped).

A day is clean only when both are no. Edging with no porn is not clean: it is
stored, it breaks the streak, and it lowers the week, and it does not write a
relapse marker. A relapse marker means porn (or that habit's slip), which an
edging-only day is not. Bricks stay a third, independent record.
"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import date, timedelta

import pytest
from conftest import csrf_token, track_porn, user_db_path
from fastapi.testclient import TestClient

from app.main import app
from app.services.freedom_log import FreedomWriteError, save_day
from mcp_server import virgil_mcp

# Wednesday. The week so far is Monday the 12th through today, three days.
# The habit starts on the 1st, so an unbroken streak is 13 days.
TODAY = date(2026, 10, 14)
KEY = {"X-API-Key": "test-key-123"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


class _Today(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _enable(client, *slugs: str) -> dict[str, int]:
    _execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_freedom', '1')")
    token = csrf_token(client, "/freedom")
    for slug in slugs or ("porn",):
        client.post("/freedom/habits", data={"slug": slug, "_csrf_token": token}, follow_redirects=False)
    return dict(_fetchall("SELECT slug, id FROM habits"))


def _day(client, slug: str, **data):
    token = csrf_token(client, f"/freedom?habit={slug}")
    return client.post(f"/freedom/{slug}/daily", data={**data, "_csrf_token": token}, follow_redirects=False)


def _brick(client, slug: str, **data):
    token = csrf_token(client, f"/freedom?habit={slug}")
    return client.post(f"/freedom/{slug}/bricks", data={**data, "_csrf_token": token}, follow_redirects=False)


def _mcp(client, name: str, arguments: dict) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    response = client.post("/mcp", headers={**MCP_HEADERS, **KEY}, json=body)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def freedom_db(tmp_path, monkeypatch):
    """Porn (edging, 5-minute slip) and alcohol (no near miss), with today frozen."""
    import app.services.streak as streak
    import app.user_db as user_db

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    monkeypatch.setattr(streak, "date", _Today)

    async def seed():
        await user_db.create_user_db("near.db")
        db = await user_db.open_user_db("near.db")
        await db.execute(
            """
            INSERT INTO habits (slug, name, slip_label, amount_unit, slip_threshold, near_miss_label, start_date)
            VALUES ('porn', 'Porn', 'I watched', 'minutes', 5, 'Edging', '2026-10-01'),
                   ('alcohol', 'Alcohol', 'I drank', 'drinks', 1, '', '2026-10-01')
            """
        )
        await db.commit()
        habits = {row["slug"]: dict(row) for row in await db.execute_fetchall("SELECT * FROM habits")}
        return db, habits

    db, habits = asyncio.run(seed())
    yield db, habits
    asyncio.run(db.close())


def _outcome(db, habit_id: int, day: str):
    async def read():
        rows = await db.execute_fetchall(
            "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?",
            (habit_id, day),
        )
        markers = await db.execute_fetchall(
            "SELECT notes FROM pmo_events WHERE habit_id = ? AND date = ? AND event_type = 'relapse'",
            (habit_id, day),
        )
        from app.services.streak import get_streak, get_week_clean

        return (
            [tuple(row) for row in rows],
            [row["notes"] for row in markers],
            await get_streak(db, habit_id),
            await get_week_clean(db, habit_id),
        )

    return asyncio.run(read())


def test_a_clean_day_with_no_edging_stays_clean_and_the_streak_continues(freedom_db):
    db, habits = freedom_db
    day = "2026-10-13"

    async def save():
        return await save_day(db, habits["porn"], day, slipped=False, amount=90, near_miss=False, note="fine")

    saved = asyncio.run(save())
    rows, markers, streak, week = _outcome(db, habits["porn"]["id"], day)

    assert saved["near_miss"] == 0 and saved["amount"] is None
    assert rows == [(0, None, 0, "fine")]
    assert markers == []
    assert streak == (13, None)
    assert week == (3, 3, 100)


def test_a_porn_slip_is_unclean_and_keeps_today_s_relapse_marker(freedom_db):
    db, habits = freedom_db
    day = "2026-10-13"

    async def save():
        return await save_day(db, habits["porn"], day, slipped=True, amount=20, near_miss=False, note="watched")

    asyncio.run(save())
    rows, markers, streak, week = _outcome(db, habits["porn"]["id"], day)

    assert rows == [(1, 20, 0, "watched")]
    assert markers == ["via daily log"]
    assert streak == (1, date(2026, 10, 13))
    assert week == (2, 3, 67)


def test_edging_only_is_stored_breaks_the_streak_and_writes_no_relapse(freedom_db):
    db, habits = freedom_db
    day = "2026-10-13"

    async def save():
        return await save_day(
            db, habits["porn"], day, slipped=False, amount=120, near_miss=True, note="about two hours"
        )

    saved = asyncio.run(save())
    rows, markers, streak, week = _outcome(db, habits["porn"]["id"], day)

    assert (saved["slipped"], saved["amount"], saved["near_miss"]) == (0, None, 1)
    assert rows == [(0, None, 1, "about two hours")]
    assert markers == []
    assert streak == (1, None), "the streak breaks, and last_relapse stays empty because this was not porn"
    assert week == (2, 3, 67)


def test_an_older_edging_day_breaks_the_streak_without_touching_this_week(freedom_db):
    db, habits = freedom_db

    async def save():
        await save_day(db, habits["porn"], "2026-10-08", slipped=False, amount=None, near_miss=True, note="last week")

    asyncio.run(save())
    _, markers, streak, week = _outcome(db, habits["porn"]["id"], "2026-10-08")

    assert markers == []
    assert streak == (6, None)
    assert week == (3, 3, 100)


def test_edging_after_an_older_relapse_breaks_the_streak_without_moving_last_relapse(freedom_db):
    db, habits = freedom_db

    async def save():
        await save_day(db, habits["porn"], "2026-10-12", slipped=True, amount=15, near_miss=False, note="monday")
        await save_day(db, habits["porn"], "2026-10-13", slipped=False, amount=None, near_miss=True, note="tuesday")

    asyncio.run(save())
    _, _, streak, week = _outcome(db, habits["porn"]["id"], "2026-10-13")

    assert streak == (1, date(2026, 10, 12))
    assert week == (1, 3, 33)


def test_porn_and_edging_on_one_day_count_once_and_keep_the_amount_rules(freedom_db):
    db, habits = freedom_db
    day = "2026-10-13"

    async def save(amount: int):
        return await save_day(db, habits["porn"], day, slipped=True, amount=amount, near_miss=True, note="both")

    with pytest.raises(FreedomWriteError, match="at least 5"):
        asyncio.run(save(4))
    assert (
        asyncio.run(db.execute_fetchall("SELECT id FROM feniks_daily WHERE habit_id = ?", (habits["porn"]["id"],)))
        == []
    )

    asyncio.run(save(5))
    rows, markers, streak, week = _outcome(db, habits["porn"]["id"], day)

    assert rows == [(1, 5, 1, "both")]
    assert markers == ["via daily log"]
    assert streak == (1, date(2026, 10, 13))
    assert week == (2, 3, 67), "a slip and edging on the same day are one unclean day"


def test_a_habit_without_a_near_miss_label_cannot_record_one(freedom_db):
    db, habits = freedom_db

    async def save():
        return await save_day(db, habits["alcohol"], "2026-10-13", slipped=False, amount=2, near_miss=True, note="")

    saved = asyncio.run(save())
    rows, markers, streak, week = _outcome(db, habits["alcohol"]["id"], "2026-10-13")

    assert saved["near_miss"] == 0
    assert rows == [(0, None, 0, "")]
    assert markers == []
    assert streak == (13, None)
    assert week == (3, 3, 100)


def test_deleting_an_edging_day_lets_the_streak_continue(freedom_db):
    db, habits = freedom_db
    from app.services.freedom_log import delete_day
    from app.services.streak import get_streak

    async def scenario():
        await save_day(db, habits["porn"], "2026-10-13", slipped=False, amount=None, near_miss=True, note="edge")
        broken = await get_streak(db, habits["porn"]["id"])
        await delete_day(db, habits["porn"], "2026-10-13")
        return broken, await get_streak(db, habits["porn"]["id"])

    assert asyncio.run(scenario()) == ((1, None), (13, None))


def test_correct_near_miss_flips_only_that_flag(freedom_db):
    """The October correction: a day already logged with no porn keeps its note and gains edging."""
    db, habits = freedom_db
    from app.services.freedom_log import correct_near_miss
    from app.services.streak import get_streak

    async def scenario():
        await db.execute(
            "INSERT INTO feniks_daily (habit_id, date, slipped, amount, near_miss, note) VALUES (?, ?, 0, NULL, 0, ?)",
            (habits["porn"]["id"], "2026-10-13", "about two hours of edging"),
        )
        await db.execute(
            "INSERT INTO feniks_bricks (habit_id, date, hook, craving, story) VALUES (?, ?, ?, ?, ?)",
            (habits["porn"]["id"], "2026-10-13", "walked", 4, "left the room"),
        )
        await db.commit()
        first = await correct_near_miss(db, habits["porn"], "2026-10-13")
        second = await correct_near_miss(db, habits["porn"], "2026-10-13")
        row = (
            await db.execute_fetchall(
                "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?",
                (habits["porn"]["id"], "2026-10-13"),
            )
        )[0]
        brick = (
            await db.execute_fetchall(
                "SELECT hook, craving, story FROM feniks_bricks WHERE habit_id = ? AND date = ?",
                (habits["porn"]["id"], "2026-10-13"),
            )
        )[0]
        markers = await db.execute_fetchall("SELECT id FROM pmo_events WHERE habit_id = ?", (habits["porn"]["id"],))
        return first, second, tuple(row), tuple(brick), markers, await get_streak(db, habits["porn"]["id"])

    first, second, row, brick, markers, streak = asyncio.run(scenario())

    assert first["changed"] is True and second["changed"] is False
    assert row == (0, None, 1, "about two hours of edging")
    assert brick == ("walked", 4, "left the room")
    assert markers == []
    assert streak == (1, None)


def test_correct_near_miss_does_not_invent_a_day_or_ignore_the_label(freedom_db):
    db, habits = freedom_db
    from app.services.freedom_log import correct_near_miss

    async def missing():
        await correct_near_miss(db, habits["porn"], "2026-10-13")

    async def unlabeled():
        await correct_near_miss(db, habits["alcohol"], "2026-10-13")

    with pytest.raises(FreedomWriteError, match="No day entry"):
        asyncio.run(missing())
    with pytest.raises(FreedomWriteError, match="does not record a near miss"):
        asyncio.run(unlabeled())
    assert asyncio.run(db.execute_fetchall("SELECT id FROM feniks_daily")) == []


def test_the_form_keeps_edging_when_i_watched_is_unchecked(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-07-20"
    try:
        response = _day(
            auth_client,
            "porn",
            date=day,
            slipped="0",
            amount="120",
            near_miss="1",
            note="about two hours",
        )
        assert response.status_code == 303
        assert "err=" not in response.headers["location"]
        assert _fetchall(
            "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
        ) == [(0, None, 1, "about two hours")]
        assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day)) == [(0,)]
    finally:
        _execute("DELETE FROM feniks_daily WHERE date = ?", (day,))
        _execute("DELETE FROM pmo_events WHERE date = ?", (day,))


def test_the_form_keeps_both_flags_when_porn_and_edging_happen_together(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-07-21"
    try:
        short = _day(auth_client, "porn", date=day, slipped="1", amount="4", near_miss="1")
        assert "err=" in short.headers["location"]
        assert _fetchall("SELECT id FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)) == []

        saved = _day(auth_client, "porn", date=day, slipped="1", amount="12", near_miss="1", note="both")
        assert "err=" not in saved.headers["location"]
        assert _fetchall(
            "SELECT slipped, amount, near_miss FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
        ) == [(1, 12, 1)]
        assert _fetchall(
            "SELECT notes FROM pmo_events WHERE habit_id = ? AND date = ? AND event_type = 'relapse'", (porn, day)
        ) == [("via daily log",)]
    finally:
        _execute("DELETE FROM feniks_daily WHERE date = ?", (day,))
        _execute("DELETE FROM pmo_events WHERE date = ?", (day,))


def test_edging_control_is_offered_for_either_answer_and_the_day_is_not_called_clean(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-07-22"
    try:
        blank = auth_client.get("/freedom?habit=porn").text
        near_at = blank.index("day-near-miss")
        amount_at = blank.index("day-amount")
        assert 'x-show="used !== null"' in blank[near_at - 200 : near_at + 200]
        assert 'name="near_miss"' in blank[near_at : near_at + 700]
        assert "x-show=\"used === '1'\"" in blank[amount_at - 200 : amount_at + 80]
        assert "near_miss" not in blank[amount_at : blank.index("</div>", amount_at)]
        assert "means this day is not clean" in blank
        assert 'x-show="!edged"' in blank

        assert _day(auth_client, "porn", date=day, slipped="0", near_miss="1", note="edged").status_code == 303
        page = auth_client.get(f"/freedom?habit=porn&date={day}").text
        at = page.index(f">{day}</a>")
        entry_start = page.rfind('<div class="timeline-day">', 0, at)
        entry_end = page.find('<div class="timeline-day">', at)
        entry = page[entry_start : entry_end if entry_end != -1 else entry_start + 1500]
        assert "timeline-badge near" in entry
        assert ">edging<" in entry
        assert "timeline-badge clean" not in entry
        assert (
            'name="near_miss"' in page
            and "checked" in page[page.index('name="near_miss"') - 80 : page.index('name="near_miss"') + 200]
        )
    finally:
        _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day))


def test_today_shows_edging_instead_of_clean_and_a_stale_clean_tap_does_not_erase_it(auth_client):
    porn = _enable(auth_client)["porn"]
    today = date.today().isoformat()
    _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, today))
    _execute("DELETE FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, today))
    try:
        assert _day(auth_client, "porn", date=today, slipped="0", near_miss="1", note="today edge").status_code == 303
        import re

        dashboard = auth_client.get("/").text
        chip = re.search(r'id="freedom-today-porn" class="freedom-status near".*?</a>', dashboard, re.S)
        assert chip, dashboard[dashboard.find("freedom-today-porn") : dashboard.find("freedom-today-porn") + 400]
        assert "Edging" in chip.group(0)
        assert "Clean" not in chip.group(0)

        token = csrf_token(auth_client, "/")
        auth_client.post("/freedom/porn/clean-today", headers={"X-CSRF-Token": token, "HX-Request": "true"})
        assert _fetchall(
            "SELECT slipped, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, today)
        ) == [(0, 1, "today edge")]
    finally:
        _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, today))
        _execute("DELETE FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, today))


def test_the_week_strip_and_week_clean_count_an_edging_day(auth_client, monkeypatch):
    import app.routers.freedom as freedom
    import app.services.streak as streak

    porn = _enable(auth_client)["porn"]
    monkeypatch.setattr(streak, "date", _Today)
    monkeypatch.setattr(freedom, "date", _Today)
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
        _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day))
        _execute("DELETE FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day))
    try:
        assert (
            _day(auth_client, "porn", date="2026-10-13", slipped="0", near_miss="1", note="tuesday").status_code == 303
        )
        page = auth_client.get("/freedom?habit=porn").text
        assert 'class="week-dot near" title="2026-10-13"' in page
        assert 'class="week-dot clean" title="2026-10-13"' not in page
        assert "This week: 2/3 clean" in page

        body = auth_client.get("/api/freedom", headers=KEY).json()
        habit = next(item for item in body["habits"] if item["habit"] == "porn")
        assert habit["week_clean"] == {"clean_days": 2, "days_elapsed": 3, "pct": 67}
    finally:
        for day in ("2026-10-12", "2026-10-13", "2026-10-14"):
            _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day))
            _execute("DELETE FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day))


def test_a_brick_stays_independent_of_the_day_flags(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-07-11"
    try:
        assert _day(auth_client, "porn", date=day, slipped="0", near_miss="1", note="edge").status_code == 303
        created = _brick(auth_client, "porn", date=day, hook="walked it off", craving="4", story="left")
        assert created.status_code == 303
        brick_id = _fetchall("SELECT id FROM feniks_bricks WHERE habit_id = ? AND hook = 'walked it off'", (porn,))[0][
            0
        ]
        token = csrf_token(auth_client, "/freedom")
        updated = auth_client.post(
            f"/freedom/bricks/{brick_id}/edit",
            data={"date": day, "hook": "walked further", "craving": "3", "story": "still left", "_csrf_token": token},
            follow_redirects=False,
        )
        assert updated.status_code == 303
        assert _fetchall(
            "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
        ) == [(0, None, 1, "edge")]
        assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
            ("walked further", 3, "still left")
        ]

        assert _day(auth_client, "porn", date=day, slipped="1", amount="10", near_miss="0").status_code == 303
        assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
            ("walked further", 3, "still left")
        ]
    finally:
        _execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day))
        _execute("DELETE FROM feniks_bricks WHERE habit_id = ? AND date = ?", (porn, day))
        _execute("DELETE FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day))


def test_mcp_log_freedom_day_keeps_edging_when_not_slipped_and_says_so(auth_client, client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    rest = TestClient(app, headers=KEY)
    rest.portal = client.portal
    monkeypatch.setattr(virgil_mcp, "_client", rest)
    porn = track_porn()
    day = "2026-07-23"
    _execute("DELETE FROM feniks_daily WHERE date = ?", (day,))
    _execute("DELETE FROM pmo_events WHERE date = ?", (day,))
    try:
        listed = client.post(
            "/mcp", headers={**MCP_HEADERS, **KEY}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        )
        tools = {tool["name"]: tool["description"] for tool in listed.json()["result"]["tools"]}
        description = tools["log_freedom_day"]
        assert "not clean" in description
        assert "does not write a relapse" in description
        assert "slipped is false" in description

        saved = _mcp(
            client,
            "log_freedom_day",
            {"habit": "porn", "date": day, "slipped": False, "near_miss": True, "amount": 120, "note": "mcp edge"},
        )
        assert (saved["slipped"], saved["amount"], saved["near_miss"], saved["note"]) == (0, None, 1, "mcp edge")
        assert _fetchall(
            "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
        ) == [(0, None, 1, "mcp edge")]
        assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day)) == [(0,)]
    finally:
        _execute("DELETE FROM feniks_daily WHERE date = ?", (day,))
        _execute("DELETE FROM pmo_events WHERE date = ?", (day,))


def test_correction_script_dry_run_writes_nothing_and_apply_sets_only_near_miss(tmp_path, monkeypatch):
    import app.user_db as user_db

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    first = (date.today() - timedelta(days=3)).isoformat()
    second = (date.today() - timedelta(days=2)).isoformat()

    async def seed():
        await user_db.create_user_db("owner.db")
        db = await user_db.open_user_db("owner.db")
        await db.execute(
            """
            INSERT INTO habits (slug, name, slip_label, near_miss_label, start_date)
            VALUES ('porn', 'Porn', 'I watched', 'Edging', '2026-01-01')
            """
        )
        habit_id = (await db.execute_fetchall("SELECT id FROM habits WHERE slug = 'porn'"))[0]["id"]
        for day, note in ((first, "about 2h edging"), (second, "about 2h edging again")):
            await db.execute(
                "INSERT INTO feniks_daily (habit_id, date, slipped, amount, near_miss, note) VALUES (?, ?, 0, NULL, 0, ?)",
                (habit_id, day, note),
            )
        await db.execute(
            "INSERT INTO feniks_bricks (habit_id, date, hook, story) VALUES (?, ?, 'kept', 'untouched')",
            (habit_id, first),
        )
        await db.commit()
        await db.close()

    asyncio.run(seed())
    database = tmp_path / "owner.db"
    script = "scripts/correct_freedom_near_miss.py"
    base = [sys.executable, script, "--database", str(database), "--habit", "porn", "--date", first, "--date", second]

    dry = subprocess.run([*base], check=False, capture_output=True, text=True)
    assert dry.returncode == 0, dry.stderr
    assert first in dry.stdout and second in dry.stdout

    def flags():
        conn = sqlite3.connect(database)
        try:
            return conn.execute(
                "SELECT date, slipped, amount, near_miss, note FROM feniks_daily ORDER BY date"
            ).fetchall()
        finally:
            conn.close()

    assert [row[3] for row in flags()] == [0, 0]

    missing = subprocess.run(
        [
            sys.executable,
            script,
            "--apply",
            "--database",
            str(database),
            "--habit",
            "porn",
            "--date",
            first,
            "--date",
            "2020-01-01",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert [row[3] for row in flags()] == [0, 0], "a missing day aborts before any write"

    applied = subprocess.run([*base, "--apply"], check=False, capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    assert flags() == [
        (first, 0, None, 1, "about 2h edging"),
        (second, 0, None, 1, "about 2h edging again"),
    ]
    conn = sqlite3.connect(database)
    try:
        assert conn.execute("SELECT COUNT(*) FROM pmo_events").fetchone()[0] == 0
        assert conn.execute("SELECT hook, story FROM feniks_bricks").fetchone() == ("kept", "untouched")
    finally:
        conn.close()

    again = subprocess.run([*base, "--apply"], check=False, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr
    assert [row[3] for row in flags()] == [1, 1]
