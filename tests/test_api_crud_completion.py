"""Reads and deletes that complete REST CRUD for the daily log, Feniks,
training sessions and body measurements."""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, user_db_path

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


def test_feniks_management_requires_the_sensitive_scope(auth_client):
    for method, path, body in (
        ("delete", "/api/feniks/daily/2026-01-01", None),
        ("patch", "/api/feniks/bricks/1", {"hook": "x"}),
        ("delete", "/api/feniks/bricks/1", None),
        ("patch", "/api/feniks/config", {"target_days": 30}),
    ):
        kwargs = {"headers": KEY} | ({"json": body} if body else {})
        assert getattr(auth_client, method)(path, **kwargs).status_code == 403, path


def test_deleting_a_feniks_day_removes_only_its_own_marker(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    day = (date.today() - timedelta(days=50)).isoformat()
    try:
        auth_client.put(f"/api/feniks/daily/{day}", json={"used": True, "minutes": 15}, headers=KEY)
        _execute("INSERT INTO pmo_events (date, event_type, notes) VALUES (?, 'reset', 'manual')", (day,))
        assert auth_client.delete(f"/api/feniks/daily/{day}", headers=KEY).status_code == 200
        assert _fetchall("SELECT COUNT(*) FROM feniks_daily WHERE date = ?", (day,)) == [(0,)]
        assert _fetchall("SELECT event_type, notes FROM pmo_events WHERE date = ?", (day,)) == [("reset", "manual")]
        assert auth_client.delete(f"/api/feniks/daily/{day}", headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM pmo_events WHERE date = ?", (day,))


def test_brick_can_be_edited_partially_and_deleted(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    day = (date.today() - timedelta(days=51)).isoformat()
    brick_id = auth_client.post(
        "/api/feniks/bricks", json={"date": day, "hook": "ZZ cold shower", "craving": 6, "story": "s"}, headers=KEY
    ).json()["id"]
    try:
        edited = auth_client.patch(f"/api/feniks/bricks/{brick_id}", json={"craving": 3}, headers=KEY)
        assert edited.status_code == 200, edited.text
        assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
            ("ZZ cold shower", 3, "s")
        ]
        blank = auth_client.patch(f"/api/feniks/bricks/{brick_id}", json={"hook": " "}, headers=KEY)
        assert blank.status_code == 422
        assert auth_client.delete(f"/api/feniks/bricks/{brick_id}", headers=KEY).status_code == 200
        assert auth_client.patch(f"/api/feniks/bricks/{brick_id}", json={"craving": 1}, headers=KEY).status_code == 404
    finally:
        _execute("DELETE FROM feniks_bricks WHERE id = ?", (brick_id,))


def test_feniks_config_is_validated_and_persisted(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    original = _fetchall("SELECT start_date, target_days, big_why FROM feniks_config WHERE id = 1")[0]
    try:
        assert auth_client.patch("/api/feniks/config", json={"target_days": 0}, headers=KEY).status_code == 422
        future = (date.today() + timedelta(days=1)).isoformat()
        assert auth_client.patch("/api/feniks/config", json={"start_date": future}, headers=KEY).status_code == 422
        resp = auth_client.patch("/api/feniks/config", json={"target_days": 30}, headers=KEY)
        assert resp.status_code == 200, resp.text
        assert _fetchall("SELECT start_date, target_days, big_why FROM feniks_config WHERE id = 1") == [
            (original[0], 30, original[2])
        ]
    finally:
        _execute("UPDATE feniks_config SET start_date = ?, target_days = ?, big_why = ? WHERE id = 1", tuple(original))


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
