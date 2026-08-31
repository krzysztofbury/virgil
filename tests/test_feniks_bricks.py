"""No Porn single-flow redesign: one day log (clean / watched + minutes/edging/note)
and bricks (urges survived: hook, craving, story) — no Journal/Pleasures tabs, one
unified timeline. A day-based streak cannot see edging, hence the daily log. The
weekly 75% clean rate (Gola) and the never-resetting counter stay untouched."""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, user_db_path


def _enable_no_porn() -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_no_porn', '1')")
        conn.commit()
    finally:
        conn.close()


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


def test_daily_log_upserts_one_row_per_date(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-20"

    resp = auth_client.post(
        "/feniks/daily",
        data={"date": day, "used": "1", "minutes": "45", "edging": "1", "note": "stres", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = auth_client.post(
        "/feniks/daily",
        data={"date": day, "used": "1", "minutes": "90", "note": "nuda", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = _fetchall("SELECT used, minutes, edging, note FROM feniks_daily WHERE date = ?", (day,))
    assert rows == [(1, 90, 0, "nuda")], "second save must update the same row; edging unchecked -> 0"


def test_daily_used_creates_relapse_event_once(auth_client):
    """used=1 must feed the existing streak/week-clean machinery (pmo_events),
    but saving the same day twice must not double-count the relapse."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-19"

    for _ in range(2):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": "1", "minutes": "5", "_csrf_token": token},
            follow_redirects=False,
        )

    rows = _fetchall("SELECT COUNT(*) FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows[0][0] == 1


def test_daily_clean_day_creates_no_relapse_event(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-18"

    auth_client.post(
        "/feniks/daily",
        data={"date": day, "used": "0", "_csrf_token": token},
        follow_redirects=False,
    )

    assert _fetchall("SELECT used FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
    rows = _fetchall("SELECT COUNT(*) FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows[0][0] == 0


def test_daily_correction_to_clean_removes_marker_relapse(auth_client):
    """A misclicked 'watched' is a normal correction: re-saving the day as clean
    must remove the relapse event the daily log itself created — otherwise
    streak/week-clean permanently contradict the daily log with no UI recovery."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-17"

    for used in ("1", "0"):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": used, "minutes": "5", "_csrf_token": token},
            follow_redirects=False,
        )

    assert _fetchall("SELECT used FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
    rows = _fetchall("SELECT COUNT(*) FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows[0][0] == 0


def test_daily_correction_preserves_foreign_relapse(auth_client):
    """Correcting the daily log to clean deletes only its own marker event,
    never a relapse recorded by another path (those carry their own notes)."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-16"

    _execute("INSERT INTO pmo_events (date, event_type, notes) VALUES (?, 'relapse', 'recorded by hand')", (day,))
    for used in ("1", "0"):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": used, "minutes": "5", "_csrf_token": token},
            follow_redirects=False,
        )

    rows = _fetchall("SELECT notes FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows == [("recorded by hand",)]


def test_brick_create_and_render(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")

    resp = auth_client.post(
        "/feniks/bricks",
        data={
            "date": date.today().isoformat(),
            "hook": "Poszedłem na spacer",
            "craving": "8",
            "story": "sam w domu po pracy; zamknąłem laptopa i wyszedłem, głód minął po 20 minutach",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = _fetchall("SELECT hook, craving FROM feniks_bricks")
    assert ("Poszedłem na spacer", 8) in rows

    html = auth_client.get("/feniks").text
    assert "Poszedłem na spacer" in html


def test_brick_requires_hook(auth_client):
    """A brick without its memory hook is not a brick (Gola: hak pamięciowy)."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    before = _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0]

    auth_client.post(
        "/feniks/bricks",
        data={"date": date.today().isoformat(), "hook": "  ", "_csrf_token": token},
        follow_redirects=False,
    )

    assert _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0] == before


def test_page_is_single_flow(auth_client):
    """One page, one decision: day choice + brick capture + unified timeline.
    No Journal/Pleasures tabs, no separate relapse form."""
    _enable_no_porn()
    html = auth_client.get("/feniks").text
    assert "Clean day" in html, "day choice must be explicit and human"
    assert "I watched" in html
    assert 'name="edging"' in html
    assert 'name="minutes"' in html
    assert "This week:" in html, "Gola weekly clean-rate stays"
    assert "Target 75%" in html
    assert "Journal Entry" not in html, "journal form is retired from the UI"
    assert "Two Pleasures" not in html, "pleasures form is retired from the UI"
    assert "Report relapse" not in html, "separate relapse form folded into the day log"
    assert "at least 5 total minutes" in html
    assert "one distinct decision episode" in html


def test_api_noporn_includes_daily_and_bricks(auth_client, monkeypatch):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    auth_client.post(
        "/feniks/bricks",
        data={"date": date.today().isoformat(), "hook": "api-test-brick", "_csrf_token": token},
        follow_redirects=False,
    )
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    resp = auth_client.get("/api/noporn", headers={"X-API-Key": "test-key-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["daily"], list)
    assert isinstance(body["bricks"], list)
    assert body["bricks_total"] >= 1
    brick = next(b for b in body["bricks"] if b["hook"] == "api-test-brick")
    assert "story" in brick and "craving" in brick


def test_brick_form_offers_the_viewed_date_and_records_it(auth_client):
    """A brick laid while reviewing an earlier day belongs to that day, not today."""
    _enable_no_porn()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    html = auth_client.get(f"/feniks?date={yesterday}").text
    assert f'<input type="date" name="date" value="{yesterday}" max="{date.today().isoformat()}"' in html
    assert f'data-draft-key="feniks-brick:{yesterday}"' in html

    token = csrf_token(auth_client, f"/feniks?date={yesterday}")
    resp = auth_client.post(
        "/feniks/bricks",
        data={"date": yesterday, "hook": "brick dated by hand", "_csrf_token": token},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/feniks?date={yesterday}")
    assert _fetchall("SELECT date FROM feniks_bricks WHERE hook = ?", ("brick dated by hand",)) == [(yesterday,)]


def test_brick_refuses_a_future_or_unparseable_date(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    before = _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0]

    for bad in ((date.today() + timedelta(days=1)).isoformat(), "not-a-date"):
        resp = auth_client.post(
            "/feniks/bricks",
            data={"date": bad, "hook": "should not land", "_csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

    assert _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0] == before


def test_brick_normalises_a_compact_iso_date(auth_client):
    """date.fromisoformat also accepts YYYYMMDD, which no longer sorts as text."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = date.today() - timedelta(days=3)

    auth_client.post(
        "/feniks/bricks",
        data={"date": day.strftime("%Y%m%d"), "hook": "compact date", "_csrf_token": token},
        follow_redirects=False,
    )

    assert _fetchall("SELECT date FROM feniks_bricks WHERE hook = ?", ("compact date",)) == [(day.isoformat(),)]


def test_clean_day_clears_watched_only_values_but_keeps_short_exposure_note(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = (date.today() - timedelta(days=4)).isoformat()

    auth_client.post(
        "/feniks/daily",
        data={
            "date": day,
            "used": "1",
            "minutes": "12",
            "edging": "1",
            "note": "watched record",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    auth_client.post(
        "/feniks/daily",
        data={
            "date": day,
            "used": "0",
            "minutes": "12",
            "edging": "1",
            "note": "brief accidental exposure under five minutes",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )

    assert _fetchall("SELECT used, minutes, edging, note FROM feniks_daily WHERE date = ?", (day,)) == [
        (0, None, 0, "brief accidental exposure under five minutes")
    ]


def test_watched_requires_at_least_five_minutes(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = (date.today() - timedelta(days=5)).isoformat()

    for minutes in ("", "4"):
        resp = auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": "1", "minutes": minutes, "_csrf_token": token},
            follow_redirects=False,
        )
        assert "err=" in resp.headers["location"]

    assert _fetchall("SELECT id FROM feniks_daily WHERE date = ?", (day,)) == []


def test_brick_can_be_corrected_and_removed(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = (date.today() - timedelta(days=2)).isoformat()
    auth_client.post(
        "/feniks/bricks",
        data={"date": day, "hook": "before correction", "craving": "9", "_csrf_token": token},
        follow_redirects=False,
    )
    brick_id = _fetchall("SELECT id FROM feniks_bricks WHERE hook = 'before correction'")[0][0]

    updated = auth_client.post(
        f"/feniks/bricks/{brick_id}/edit",
        data={
            "date": day,
            "hook": "after correction",
            "craving": "6",
            "story": "corrected detail",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
        ("after correction", 6, "corrected detail")
    ]

    removed = auth_client.post(
        f"/feniks/bricks/{brick_id}/delete",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert _fetchall("SELECT id FROM feniks_bricks WHERE id = ?", (brick_id,)) == []
