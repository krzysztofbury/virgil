"""Reads and deletes that complete REST CRUD for the daily log, Feniks,
training sessions and body measurements."""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, track_porn, user_db_path

KEY = {"X-API-Key": "test-key-123"}


def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(user_db_path()) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(sql: str, params: tuple = ()) -> None:
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute(sql, params)


def test_daily_log_can_be_read_and_deleted(auth_client):
    day = "2026-01-12"
    assert auth_client.get(f"/api/daily/{day}", headers=KEY).status_code == 404
    auth_client.put(f"/api/daily/{day}", json={"andy_body_desc": "ZZ swim"}, headers=KEY)
    assert auth_client.get(f"/api/daily/{day}", headers=KEY).json()["andy_body_desc"] == "ZZ swim"
    assert auth_client.delete(f"/api/daily/{day}", headers=KEY).status_code == 200
    assert auth_client.delete(f"/api/daily/{day}", headers=KEY).status_code == 404
    assert _fetchall("SELECT COUNT(*) FROM daily_logs WHERE date = ?", (day,)) == [(0,)]


def test_deleting_a_freedom_day_removes_only_its_own_marker(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    porn = track_porn()
    day = (date.today() - timedelta(days=50)).isoformat()
    try:
        auth_client.put(f"/api/freedom/porn/days/{day}", json={"slipped": True, "amount": 15}, headers=KEY)
        _execute(
            "INSERT INTO pmo_events (habit_id, date, event_type, notes) VALUES (?, ?, 'reset', 'manual')", (porn, day)
        )
        assert auth_client.delete(f"/api/freedom/porn/days/{day}", headers=KEY).status_code == 200
        assert _fetchall("SELECT COUNT(*) FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
        assert _fetchall("SELECT event_type, notes FROM pmo_events WHERE date = ?", (day,)) == [("reset", "manual")]
        assert auth_client.delete(f"/api/freedom/porn/days/{day}", headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM pmo_events WHERE date = ?", (day,))


def test_brick_can_be_edited_partially_and_deleted(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    track_porn()
    day = (date.today() - timedelta(days=51)).isoformat()
    brick_id = auth_client.post(
        "/api/freedom/porn/bricks",
        json={"date": day, "hook": "ZZ cold shower", "craving": 6, "story": "s"},
        headers=KEY,
    ).json()["id"]
    try:
        edited = auth_client.patch(f"/api/freedom/bricks/{brick_id}", json={"craving": 3}, headers=KEY)
        assert edited.status_code == 200, edited.text
        assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
            ("ZZ cold shower", 3, "s")
        ]
        blank = auth_client.patch(f"/api/freedom/bricks/{brick_id}", json={"hook": " "}, headers=KEY)
        assert blank.status_code == 422
        assert auth_client.delete(f"/api/freedom/bricks/{brick_id}", headers=KEY).status_code == 200
        assert auth_client.patch(f"/api/freedom/bricks/{brick_id}", json={"craving": 1}, headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM feniks_bricks WHERE id = ?", (brick_id,))


def test_habit_settings_are_validated_and_persisted(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    porn = track_porn()
    fields = "start_date, target_days, big_why, slip_label, slip_threshold"
    original = _fetchall(f"SELECT {fields} FROM habits WHERE id = ?", (porn,))[0]  # noqa: S608 - fixed columns
    try:
        assert auth_client.patch("/api/freedom/porn", json={"target_days": 0}, headers=KEY).status_code == 422
        future = (date.today() + timedelta(days=1)).isoformat()
        assert auth_client.patch("/api/freedom/porn", json={"start_date": future}, headers=KEY).status_code == 422
        assert auth_client.patch("/api/freedom/no-such-habit", json={"target_days": 30}, headers=KEY).status_code == 404
        resp = auth_client.patch(
            "/api/freedom/porn", json={"target_days": 30, "slip_label": "I looked", "slip_threshold": 10}, headers=KEY
        )
        assert resp.status_code == 200, resp.text
        assert _fetchall(f"SELECT {fields} FROM habits WHERE id = ?", (porn,)) == [  # noqa: S608 - fixed columns
            (original[0], 30, original[2], "I looked", 10)
        ]
        # The deprecated config path still changes the first tracked habit.
        assert auth_client.patch("/api/feniks/config", json={"target_days": 45}, headers=KEY).status_code == 200
        assert _fetchall("SELECT target_days FROM habits WHERE id = ?", (porn,)) == [(45,)]
    finally:
        _execute(
            f"UPDATE habits SET {', '.join(f'{f} = ?' for f in fields.split(', '))} WHERE id = ?",  # noqa: S608
            (*original, porn),
        )


def test_single_training_session_can_be_read(auth_client):
    created = auth_client.post(
        "/api/training/sessions", json={"date": "2026-01-13", "notes": "ZZ single read"}, headers=KEY
    ).json()
    try:
        resp = auth_client.get(f"/api/training/sessions/{created['id']}", headers=KEY)
        assert resp.status_code == 200
        assert resp.json()["notes"] == "ZZ single read"
        assert auth_client.get("/api/training/sessions/999999", headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM training_sessions WHERE id = ?", (created["id"],))


def test_measurements_merge_validate_list_and_delete(auth_client):
    day = (date.today() - timedelta(days=2)).isoformat()
    try:
        assert auth_client.put(f"/api/measurements/{day}", json={"weight": 99.5}, headers=KEY).status_code == 200
        merged = auth_client.put(f"/api/measurements/{day}", json={"waist": 98}, headers=KEY).json()
        assert (merged["weight"], merged["waist"]) == (99.5, 98.0)
        assert auth_client.put(f"/api/measurements/{day}", json={"weight": 5}, headers=KEY).status_code == 422
        listed = auth_client.get("/api/measurements", params={"range": 7}, headers=KEY).json()["measurements"]
        assert any(row["date"] == day and row["weight"] == 99.5 for row in listed)
        assert auth_client.delete(f"/api/measurements/{day}", headers=KEY).status_code == 200
        assert auth_client.delete(f"/api/measurements/{day}", headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM body_measurements WHERE date = ?", (day,))


def test_measurement_form_refuses_the_same_values_as_rest(auth_client):
    day = "2026-01-14"
    token = csrf_token(auth_client, f"/daily/{day}")
    resp = auth_client.post(
        "/daily/measurements", data={"date": day, "weight": "5", "_csrf_token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _fetchall("SELECT COUNT(*) FROM body_measurements WHERE date = ?", (day,)) == [(0,)]
