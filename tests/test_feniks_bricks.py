"""No Porn: bricks (urges survived) become the visible progress unit, and a
short daily log (used / minutes / edging) complements day-counting — a day-based
streak cannot see edging. Gola structure (journal, pleasures, weekly 75% clean
rate that never resets) stays untouched."""

import sqlite3
from datetime import date

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


def test_daily_log_upserts_one_row_per_date(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-20"

    resp = auth_client.post(
        "/feniks/daily",
        data={"date": day, "used": "1", "minutes": "45", "edging": "1", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = auth_client.post(
        "/feniks/daily",
        data={"date": day, "used": "1", "minutes": "90", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = _fetchall("SELECT used, minutes, edging FROM feniks_daily WHERE date = ?", (day,))
    assert rows == [(1, 90, 0)], "second save must update the same row (upsert), edging unchecked -> 0"


def test_daily_used_creates_relapse_event_once(auth_client):
    """used=1 must feed the existing streak/week-clean machinery (pmo_events),
    but saving the same day twice must not double-count the relapse."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-19"

    for _ in range(2):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": "1", "_csrf_token": token},
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
        data={"date": day, "minutes": "0", "_csrf_token": token},
        follow_redirects=False,
    )

    assert _fetchall("SELECT used FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
    rows = _fetchall("SELECT COUNT(*) FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows[0][0] == 0


def test_daily_correction_to_clean_removes_marker_relapse(auth_client):
    """A misclicked 'used' checkbox is a normal correction: re-saving the day as
    clean must remove the relapse event the daily log itself created — otherwise
    streak/week-clean permanently contradict the daily log with no UI recovery."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-17"

    for used in ("1", ""):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": used, "_csrf_token": token},
            follow_redirects=False,
        )

    assert _fetchall("SELECT used FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
    rows = _fetchall("SELECT COUNT(*) FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows[0][0] == 0


def test_daily_correction_preserves_manual_relapse(auth_client):
    """Correcting the daily log to clean deletes only its own marker event,
    never a relapse the user reported manually (those carry their own notes)."""
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")
    day = "2026-08-16"

    auth_client.post(
        "/feniks/relapse",
        data={"date": day, "notes": "reported by hand", "_csrf_token": token},
        follow_redirects=False,
    )
    for used in ("1", ""):
        auth_client.post(
            "/feniks/daily",
            data={"date": day, "used": used, "_csrf_token": token},
            follow_redirects=False,
        )

    rows = _fetchall("SELECT notes FROM pmo_events WHERE event_type = 'relapse' AND date = ?", (day,))
    assert rows == [("reported by hand",)]


def test_brick_create_and_render(auth_client):
    _enable_no_porn()
    token = csrf_token(auth_client, "/feniks")

    resp = auth_client.post(
        "/feniks/bricks",
        data={
            "date": date.today().isoformat(),
            "hook": "Poszedłem na spacer",
            "situation": "sam w domu po pracy",
            "craving": "8",
            "action": "zamknąłem laptopa i wyszedłem",
            "lesson": "głód minął po 20 minutach",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    rows = _fetchall("SELECT hook, craving FROM feniks_bricks")
    assert ("Poszedłem na spacer", 8) in rows

    html = auth_client.get("/feniks/bricks").text
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


def test_page_hero_counts_bricks_and_keeps_gola_elements(auth_client):
    _enable_no_porn()
    html = auth_client.get("/feniks").text
    assert "bricks" in html.lower(), "bricks must be the visible progress unit"
    assert "This week:" in html, "Gola weekly clean-rate stays"
    assert "Target 75%" in html
    assert 'name="edging"' in html, "daily log form must expose the edging field"
    assert 'name="minutes"' in html


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
