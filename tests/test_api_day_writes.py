"""REST writes for the daily log, Feniks and training sessions.

A check-in agent records a day through these routes instead of the forms, so
each one must keep the form path's data contract: a partial daily write merges,
a Feniks day keeps its relapse marker consistent, and a training session either
lands with every set or not at all.
"""

import sqlite3
from datetime import date, timedelta

from conftest import user_db_path

KEY = {"X-API-Key": "test-key-123"}


def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(user_db_path()) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute(sql, params)


def test_daily_put_merges_partial_fields_into_the_stored_day(auth_client):
    day = "2026-01-03"
    try:
        first = auth_client.put(
            f"/api/daily/{day}",
            json={"energy": 6, "andy_body_desc": "CrossFit 9:00", "andy_body_status": "pending"},
            headers=KEY,
        )
        assert first.status_code == 200, first.text

        second = auth_client.put(f"/api/daily/{day}", json={"andy_body_status": "done"}, headers=KEY)
        assert second.status_code == 200, second.text
        assert _fetchall(
            "SELECT energy, andy_body_status, andy_body_desc, water FROM daily_logs WHERE date = ?", (day,)
        ) == [(6, "done", "CrossFit 9:00", "pending")]

        cleared = auth_client.put(f"/api/daily/{day}", json={"energy": None}, headers=KEY)
        assert cleared.json()["energy"] is None
    finally:
        _execute("DELETE FROM daily_logs WHERE date = ?", (day,))


def test_daily_put_rejects_invalid_values_without_writing(auth_client):
    day = "2026-01-04"
    for body in ({"water": "maybe"}, {"energy": 11}, {"andy_spirit_desc": "x" * 501}, {}):
        resp = auth_client.put(f"/api/daily/{day}", json=body, headers=KEY)
        assert resp.status_code == 422, (body, resp.text)
    assert auth_client.put("/api/daily/2026-02-30", json={"energy": 5}, headers=KEY).status_code == 422
    assert _fetchall("SELECT COUNT(*) FROM daily_logs WHERE date = ?", (day,)) == [(0,)]


def test_feniks_writes_require_the_sensitive_scope(auth_client):
    day = (date.today() - timedelta(days=40)).isoformat()
    assert auth_client.put(f"/api/feniks/daily/{day}", json={"used": False}, headers=KEY).status_code == 403
    brick = {"date": day, "hook": "closed the tab"}
    assert auth_client.post("/api/feniks/bricks", json=brick, headers=KEY).status_code == 403
    assert _fetchall("SELECT COUNT(*) FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]


def test_feniks_day_keeps_relapse_marker_in_step_with_the_outcome(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    day = (date.today() - timedelta(days=41)).isoformat()
    other = (date.today() - timedelta(days=42)).isoformat()
    try:
        watched = auth_client.put(
            f"/api/feniks/daily/{day}", json={"used": True, "minutes": 20, "edging": True}, headers=KEY
        )
        assert watched.status_code == 200, watched.text
        assert _fetchall("SELECT notes FROM pmo_events WHERE date = ? AND event_type = 'relapse'", (day,)) == [
            ("via daily log",)
        ]

        clean = auth_client.put(f"/api/feniks/daily/{day}", json={"used": False, "minutes": 20}, headers=KEY)
        assert clean.status_code == 200
        assert _fetchall("SELECT used, minutes, edging FROM feniks_daily WHERE date = ?", (day,)) == [(0, None, 0)]
        assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE date = ?", (day,)) == [(0,)]

        # A relapse recorded by another path is history the day log must not erase.
        _execute("INSERT INTO pmo_events (date, event_type, notes) VALUES (?, 'relapse', 'manual')", (other,))
        auth_client.put(f"/api/feniks/daily/{other}", json={"used": False}, headers=KEY)
        assert _fetchall("SELECT notes FROM pmo_events WHERE date = ?", (other,)) == [("manual",)]
    finally:
        for d in (day, other):
            _execute("DELETE FROM feniks_daily WHERE date = ?", (d,))
            _execute("DELETE FROM pmo_events WHERE date = ?", (d,))


def test_feniks_day_refuses_short_watched_and_future_days(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    day = (date.today() - timedelta(days=43)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    short = auth_client.put(f"/api/feniks/daily/{day}", json={"used": True, "minutes": 3}, headers=KEY)
    assert short.status_code == 422
    assert "5 total minutes" in short.json()["detail"]
    assert auth_client.put(f"/api/feniks/daily/{tomorrow}", json={"used": False}, headers=KEY).status_code == 422
    assert _fetchall("SELECT COUNT(*) FROM feniks_daily WHERE date IN (?, ?)", (day, tomorrow)) == [(0,)]


def test_brick_requires_a_hook_and_stores_its_own_date(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    day = (date.today() - timedelta(days=44)).isoformat()
    try:
        blank = auth_client.post("/api/feniks/bricks", json={"date": day, "hook": "  "}, headers=KEY)
        assert blank.status_code == 422
        resp = auth_client.post(
            "/api/feniks/bricks", json={"date": day, "hook": "walked the dog", "craving": 14}, headers=KEY
        )
        assert resp.status_code == 201, resp.text
        assert _fetchall("SELECT hook, craving FROM feniks_bricks WHERE date = ?", (day,)) == [("walked the dog", 10)]
    finally:
        _execute("DELETE FROM feniks_bricks WHERE date = ?", (day,))


def _library_names(count: int) -> list[str]:
    rows = _fetchall("SELECT name FROM exercise_library WHERE archived = 0 ORDER BY id LIMIT ?", (count,))
    assert len(rows) == count, "the seeded exercise library must provide movements"
    return [row[0] for row in rows]


def test_training_session_lands_with_every_set_and_replays_by_key(auth_client):
    first_move, second_move = _library_names(2)
    payload = {
        "date": "2026-01-05",
        "duration_minutes": 60,
        "notes": "ZZ api session",
        "entries": [
            {"movement": first_move, "set_number": 1, "reps": 5, "weight": 60},
            {"movement": second_move.lower(), "set_number": 1, "duration": 90},
        ],
        "idempotency_key": "zz-training-1",
    }
    created = auth_client.post("/api/training/sessions", json=payload, headers=KEY)
    assert created.status_code == 200, created.text
    body = created.json()
    session_id = body["id"]
    try:
        assert body["created"] is True
        assert [(e["movement"], e["reps"], e["weight"], e["duration"]) for e in body["entries"]] == [
            (first_move, 5, 60.0, None),
            (second_move, None, None, 90.0),
        ]

        replay = auth_client.post("/api/training/sessions", json=payload, headers=KEY).json()
        assert (replay["id"], replay["created"]) == (session_id, False)
        conflict = auth_client.post("/api/training/sessions", json={**payload, "notes": "other"}, headers=KEY)
        assert conflict.status_code == 409
        assert _fetchall("SELECT COUNT(*) FROM training_sessions WHERE date = '2026-01-05'") == [(1,)]
    finally:
        _execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
        _execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))


def test_training_session_with_an_unknown_movement_writes_nothing(auth_client):
    (known,) = _library_names(1)
    before = _fetchall("SELECT COUNT(*) FROM training_sessions")
    resp = auth_client.post(
        "/api/training/sessions",
        json={
            "date": "2026-01-06",
            "entries": [{"movement": known, "reps": 5}, {"movement": "ZZ Moon Jump", "reps": 3}],
        },
        headers=KEY,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["unknown_movements"] == ["ZZ Moon Jump"]
    assert _fetchall("SELECT COUNT(*) FROM training_sessions") == before

    out_of_range = auth_client.post(
        "/api/training/sessions",
        json={"date": "2026-01-06", "entries": [{"movement": known, "reps": 5000}]},
        headers=KEY,
    )
    assert out_of_range.status_code == 422
    assert _fetchall("SELECT COUNT(*) FROM training_sessions") == before


def test_training_patch_replaces_sets_and_settles_a_pending_parse(auth_client):
    first_move, second_move = _library_names(2)
    created = auth_client.post(
        "/api/training/sessions",
        json={"date": "2026-01-07", "notes": "ZZ patch me", "entries": [{"movement": first_move, "reps": 1}]},
        headers=KEY,
    ).json()
    session_id = created["id"]
    _execute("UPDATE training_sessions SET wod_parsed = '{\"entries\": []}' WHERE id = ?", (session_id,))
    try:
        patched = auth_client.patch(
            f"/api/training/sessions/{session_id}",
            json={"duration_minutes": 45, "entries": [{"movement": second_move, "set_number": 2, "reps": 8}]},
            headers=KEY,
        )
        assert patched.status_code == 200, patched.text
        assert [(e["movement"], e["set_number"], e["reps"]) for e in patched.json()["entries"]] == [(second_move, 2, 8)]
        assert _fetchall(
            "SELECT duration_minutes, notes, wod_parsed FROM training_sessions WHERE id = ?", (session_id,)
        ) == [(45, "ZZ patch me", None)]

        failed = auth_client.patch(
            f"/api/training/sessions/{session_id}", json={"entries": [{"movement": "ZZ Moon Jump"}]}, headers=KEY
        )
        assert failed.status_code == 422
        assert _fetchall("SELECT COUNT(*) FROM training_entries WHERE session_id = ?", (session_id,)) == [(1,)]

        assert auth_client.delete(f"/api/training/sessions/{session_id}", headers=KEY).status_code == 200
        assert auth_client.delete(f"/api/training/sessions/{session_id}", headers=KEY).status_code == 404
        assert auth_client.patch("/api/training/sessions/999999", json={"notes": "x"}, headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
        _execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
