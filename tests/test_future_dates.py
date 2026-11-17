"""Days after today are refused on the daily log, measurements and life scores,
and the streak and "latest" reads stop at today, as Freedom does (#240).

One accidental tap on tomorrow's daily page used to store a pending day that
reset every habit streak, and a measurement or life score dated ahead became
the dashboard's "latest".
"""

import re
import sqlite3
from datetime import date, timedelta

import pytest
from conftest import csrf_token, user_db_path

KEY = {"X-API-Key": "test-key-123"}
TABLES = ("daily_logs", "body_measurements", "life_scores")


def _day(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


@pytest.fixture
def db():
    """The three tables emptied for the test and restored after it."""
    conn = sqlite3.connect(user_db_path())
    saved = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in TABLES}  # noqa: S608
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
    conn.commit()
    yield conn
    for table, rows in saved.items():
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
        if rows:
            conn.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)  # noqa: S608
    conn.commit()
    conn.close()


def _count(conn, table: str, day: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE date = ?", (day,)).fetchone()[0]  # noqa: S608


API_WRITES = [
    ("/api/daily/{day}", {"water": "done"}, "daily_logs"),
    ("/api/measurements/{day}", {"weight": 80.0}, "body_measurements"),
    ("/api/life-scores/{day}", {"health": 7}, "life_scores"),
]


@pytest.mark.parametrize(("path", "body", "table"), API_WRITES, ids=[w[2] for w in API_WRITES])
def test_an_api_write_for_a_day_after_today_is_refused(auth_client, db, path, body, table):
    for day in (_day(1), _day(400), "2099-12-31"):
        resp = auth_client.put(path.format(day=day), json=body, headers=KEY)
        assert resp.status_code == 422, (day, resp.text)
        assert "today or earlier" in resp.json()["detail"]
    assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608

    # Today is the boundary and still accepted.
    assert auth_client.put(path.format(day=_day(0)), json=body, headers=KEY).status_code == 200
    assert _count(db, table, _day(0)) == 1


FORM_WRITES = [
    ("/daily/save", {"water": "done"}, "daily_logs"),
    ("/daily/measurements", {"weight": "80"}, "body_measurements"),
    ("/life-scores/save", {"health": "7"}, "life_scores"),
]


@pytest.mark.parametrize(("path", "fields", "table"), FORM_WRITES, ids=[w[2] for w in FORM_WRITES])
def test_a_form_save_for_a_day_after_today_is_refused(auth_client, db, path, fields, table):
    token = csrf_token(auth_client, "/daily")
    for headers in ({}, {"HX-Request": "true"}):
        resp = auth_client.post(
            path, data={**fields, "date": _day(1), "_csrf_token": token}, headers=headers, follow_redirects=False
        )
        assert resp.status_code in (200, 303), resp.text
        assert "err=" in (resp.headers.get("location") or resp.headers.get("HX-Redirect") or ""), resp.headers
    assert _count(db, table, _day(1)) == 0


def test_a_tap_on_tomorrow_changes_nothing(auth_client, db):
    token = csrf_token(auth_client, "/")
    resp = auth_client.post(
        f"/daily/{_day(1)}/cycle", data={"field": "water"}, headers={"X-CSRF-Token": token, "HX-Request": "true"}
    )
    assert resp.status_code == 422
    assert _count(db, "daily_logs", _day(1)) == 0


def _water_streak(html: str) -> int:
    match = re.search(r'<div class="stat-value"[^>]*>(\d+)</div>\s*<div class="stat-label">Water</div>', html)
    assert match, "the Water streak card"
    return int(match.group(1))


@pytest.mark.parametrize(("today_water", "streak"), [("pending", 3), ("done", 4)])
def test_a_stored_day_after_today_does_not_reset_a_streak(auth_client, db, today_water, streak):
    """Rows dated ahead may already exist from before the fix, and A.N.D.Y. used to
    store one; the streak must count back from today regardless, and today itself
    is inside the bound."""
    for offset in (-1, -2, -3):
        db.execute("INSERT INTO daily_logs (date, water) VALUES (?, 'done')", (_day(offset),))
    db.execute("INSERT INTO daily_logs (date, water) VALUES (?, ?)", (_day(0), today_water))
    db.execute("INSERT INTO daily_logs (date, water) VALUES (?, 'pending')", (_day(1),))
    db.commit()
    # A pending today neither counts nor breaks (the day is not over); a done today counts.
    assert _water_streak(auth_client.get("/daily").text) == streak


def test_the_daily_page_stops_at_today(auth_client, db):
    resp = auth_client.get(f"/daily/{_day(1)}", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/daily"

    today = auth_client.get("/daily").text
    assert f'href="/daily/{_day(1)}"' not in today
    assert f'href="/daily/{_day(-1)}"' in today
    assert f'href="/daily/{_day(0)}"' in auth_client.get(f"/daily/{_day(-1)}").text


def _weight_on_dashboard(html: str) -> str:
    match = re.search(r'<div class="stat-value">([^<]*)</div>\s*<div class="stat-label">Weight \(kg\)</div>', html)
    assert match, "the Weight card"
    return match.group(1)


def test_a_future_measurement_or_life_score_never_leads_the_dashboard(auth_client, db):
    """Today is inside the bound and leads; a row dated ahead never does."""
    for offset, weight in ((-2, 81.5), (0, 82.25), (3, 199.5)):
        db.execute("INSERT INTO body_measurements (date, weight) VALUES (?, ?)", (_day(offset), weight))
    for offset, power in ((-2, 6.0), (0, 7.5), (3, 9.0)):
        db.execute("INSERT INTO life_scores (date, power_level, health) VALUES (?, ?, 5)", (_day(offset), power))
    db.commit()

    html = auth_client.get("/").text
    assert _weight_on_dashboard(html) == "82.25"
    assert "199.5" not in html
    section = html[html.index('id="life-scores"') :]
    assert '<div class="life-power">7.5</div>' in section
    assert f"<strong>{_day(0)}</strong>" in section
    assert _day(3) not in section
    assert f'max="{_day(0)}"' in section, "the life-score date field stops at today"

    summary = auth_client.get("/api/summary", headers=KEY).json()
    assert summary["measurements_latest"]["weight"] == 82.25
    measurements = auth_client.get("/api/measurements", headers=KEY).json()["measurements"]
    assert [row["date"] for row in measurements] == [_day(0), _day(-2)]
    scores = auth_client.get("/api/life-scores", headers=KEY).json()["assessments"]
    assert [row["date"] for row in scores] == [_day(0), _day(-2)]


def test_the_life_score_weight_fallback_stops_at_today(auth_client, db):
    """With no body measurements the dashboard falls back to life-score weights."""
    for offset, weight in ((-2, 81.5), (0, 82.25), (3, 199.5)):
        db.execute("INSERT INTO life_scores (date, weight) VALUES (?, ?)", (_day(offset), weight))
    db.commit()
    html = auth_client.get("/").text
    assert _weight_on_dashboard(html) == "82.25"
    assert "199.5" not in html


def test_the_habit_range_read_stops_at_today(auth_client, db):
    for offset, water in ((-1, "done"), (0, "done"), (1, "pending")):
        db.execute("INSERT INTO daily_logs (date, water) VALUES (?, ?)", (_day(offset), water))
    db.commit()
    logs = auth_client.get("/api/habits", headers=KEY).json()["logs"]
    assert [row["date"] for row in logs] == [_day(0), _day(-1)]


DONE_DAY = (
    "INSERT INTO daily_logs (date, energy, morning_routine, evening_routine, water, andy_body_status, "
    "andy_spirit_status, andy_account_status, andy_relations_status) "
    "VALUES (?, ?, 'done', 'done', 'done', 'done', 'done', 'done', 'done')"
)


def test_the_week_strip_and_year_calendar_stop_at_today(auth_client, db):
    """A day already stored ahead shows as not logged; today's own day still counts."""
    today = date.today()
    ahead = {_day(1), date(today.year, 12, 31).isoformat()} - {_day(0)}
    db.execute(DONE_DAY, (_day(0), 6))
    for day in ahead:
        db.execute(DONE_DAY, (day, 9))
    db.commit()

    html = auth_client.get("/").text
    assert f"{_day(0)}: energy 6, 7 of 7 habits done" in html
    assert f"{_day(0)}: 100%" in html
    for day in ahead:
        assert f"{day}: 100%" not in html, f"the year calendar shows {day}"
    if today.weekday() < 6:  # tomorrow is still in this week's strip
        assert f"{_day(1)}: energy not logged, 0 of 7 habits done" in html
    assert "energy 9" not in html


def test_suggestions_for_a_day_after_today_are_not_queued(auth_client, db):
    token = csrf_token(auth_client, "/daily")
    resp = auth_client.post(
        "/daily/generate-andy",
        data={"date": _day(1), "job_nonce": "a" * 32, "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "today+or+earlier" in resp.headers["location"]
