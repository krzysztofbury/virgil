"""REST API: auth paths and endpoint contracts."""

import sqlite3

from conftest import user_db_path

KEY = {"X-API-Key": "test-key-123"}


def test_no_key_401(client):
    assert client.get("/api/summary").status_code == 401


def test_wrong_key_401(client):
    assert client.get("/api/summary", headers={"X-API-Key": "wrong"}).status_code == 401


def test_disabled_api_403(client, monkeypatch):
    import app.routers.api as api_module

    monkeypatch.setattr(api_module, "API_KEY", "")
    assert client.get("/api/summary", headers=KEY).status_code == 403


def test_unknown_api_user_503(client, monkeypatch):
    import app.routers.api as api_module

    monkeypatch.setattr(api_module, "API_USER_EMAIL", "ghost@example.com")
    assert client.get("/api/summary", headers=KEY).status_code == 503


def test_summary_shape(auth_client):
    resp = auth_client.get("/api/summary", headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("date", "daily", "feniks", "oura_latest", "training_week", "measurements_latest"):
        assert key in body
    assert "streak_days" in body["feniks"]


def test_habits_range(auth_client):
    resp = auth_client.get("/api/habits?range=7", headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["range_days"] == 7
    assert isinstance(body["logs"], list)


def test_habits_range_out_of_bounds_422(auth_client):
    assert auth_client.get("/api/habits?range=999", headers=KEY).status_code == 422


def test_training_empty(auth_client):
    resp = auth_client.get("/api/training", headers=KEY)
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


def test_experiments_active(auth_client):
    resp = auth_client.get("/api/experiments/active", headers=KEY)
    assert resp.status_code == 200
    assert "experiments" in resp.json()


def test_oura_today_404_when_no_data(auth_client):
    assert auth_client.get("/api/oura/today", headers=KEY).status_code == 404


def test_all_api_routes_are_public_paths():
    """Every X-API-Key route MUST be in PUBLIC_PATHS. Otherwise AuthMiddleware 303-redirects
    it to /login before the key check — which is exactly how get_noporn/get_training_detail
    broke: the route existed but was blocked upstream (see app/auth.py PUBLIC_PATHS comment)."""
    from app.auth import PUBLIC_PATHS
    from app.routers.api import router

    api_paths = {r.path for r in router.routes}  # r.path already includes the /api prefix
    assert api_paths <= PUBLIC_PATHS, f"API routes missing from PUBLIC_PATHS: {api_paths - PUBLIC_PATHS}"


def test_new_api_routes_reachable_with_key_only():
    """MCP/curl send only X-API-Key (no session cookie). A whitelisted route reaches the key
    check (401 without a key); a route blocked by AuthMiddleware would 302/303 to /login."""
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app)
    for path in ("/api/noporn", "/api/training/detail"):
        assert c.get(path, follow_redirects=False).status_code == 401, path


def test_training_detail_empty(auth_client):
    resp = auth_client.get("/api/training/detail", headers=KEY)
    assert resp.status_code == 200
    assert resp.json()["sessions"] == []


def test_training_detail_range_out_of_bounds_422(auth_client):
    assert auth_client.get("/api/training/detail?range=999", headers=KEY).status_code == 422


def test_noporn_403_without_sensitive_scope(auth_client):
    """Intimate journal content is opt-in — default deployment must refuse it."""
    resp = auth_client.get("/api/noporn", headers=KEY)
    assert resp.status_code == 403


def test_noporn_shape(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    resp = auth_client.get("/api/noporn", headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "range_days",
        "since",
        "config",
        "streak_days",
        "last_relapse",
        "week_clean",
        "events",
        "journal",
        "pleasures",
    ):
        assert key in body
    assert isinstance(body["events"], list)
    assert isinstance(body["journal"], list)
    assert {"clean_days", "days_elapsed", "pct"} <= body["week_clean"].keys()


def test_noporn_range_out_of_bounds_422(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    assert auth_client.get("/api/noporn?range=999", headers=KEY).status_code == 422


def test_noporn_and_training_detail_with_data(auth_client, monkeypatch):
    """Seed a relapse + journal + a session with a reps and a timed exercise; assert the
    endpoints surface them (grouped, with duration on the timed lift). Cleans up after
    itself so the shared session DB stays empty for other tests."""
    monkeypatch.setattr("app.config.API_SENSITIVE", True)
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO training_exercises (name, section, target_reps, display_order, metric) "
            "VALUES ('Goblet Squat', 'Core', '10-12', 1, 'reps')"
        )
        reps_ex = cur.lastrowid
        cur.execute(
            "INSERT INTO training_exercises (name, section, target_reps, display_order, metric) "
            "VALUES ('Farmer Carry', 'Core', '40s', 2, 'time')"
        )
        time_ex = cur.lastrowid
        cur.execute("INSERT INTO training_sessions (date, duration_minutes, notes) VALUES ('2026-07-06', 25, 'test')")
        sid = cur.lastrowid
        cur.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
            "VALUES (?,?,?,?,?,?)",
            [
                (sid, reps_ex, 1, 12, 20.0, None),
                (sid, reps_ex, 2, 10, 20.0, None),
                (sid, time_ex, 1, None, 16.0, 40.0),
            ],
        )
        cur.execute("INSERT INTO pmo_events (date, event_type, notes) VALUES ('2026-07-05', 'relapse', 'tired, bored')")
        cur.execute(
            "INSERT INTO feniks_journal (date, emotions, triggers) VALUES ('2026-07-05', 'flat', 'evening downtime')"
        )
        conn.commit()

        detail = auth_client.get("/api/training/detail?range=90", headers=KEY).json()
        sess = next(s for s in detail["sessions"] if s["id"] == sid)
        by_name = {e["name"]: e for e in sess["exercises"]}
        assert len(by_name["Goblet Squat"]["sets"]) == 2
        assert by_name["Goblet Squat"]["metric"] == "reps"
        carry = by_name["Farmer Carry"]
        assert carry["metric"] == "time"
        assert carry["sets"][0]["duration"] == 40.0
        assert carry["sets"][0]["reps"] is None

        noporn = auth_client.get("/api/noporn?range=90", headers=KEY).json()
        dates = {e["date"] for e in noporn["events"]}
        assert "2026-07-05" in dates
        relapse = next(e for e in noporn["events"] if e["date"] == "2026-07-05")
        assert relapse["event_type"] == "relapse"
        assert relapse["notes"] == "tired, bored"
        assert any(j["triggers"] == "evening downtime" for j in noporn["journal"])
    finally:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM training_entries WHERE session_id = (SELECT id FROM training_sessions WHERE notes='test')"
        )
        cur.execute("DELETE FROM training_sessions WHERE notes = 'test'")
        cur.execute("DELETE FROM training_exercises WHERE name IN ('Goblet Squat', 'Farmer Carry')")
        cur.execute("DELETE FROM pmo_events WHERE date = '2026-07-05'")
        cur.execute("DELETE FROM feniks_journal WHERE date = '2026-07-05'")
        conn.commit()
        conn.close()
