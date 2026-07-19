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


# --- General experiments: per-metric progress + the API's single write ---


def _seed_api_experiment(exp_title="API Exp"):
    """Insert an active experiment with a count metric (target 8 total) directly."""
    from datetime import date

    conn = sqlite3.connect(user_db_path())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO experiments (title, start_date, num_weeks, status) VALUES (?, ?, 2, 'active')",
        (exp_title, date.today().isoformat()),
    )
    exp_id = cur.lastrowid
    cur.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period) "
        "VALUES (?, 'Gate executed', '#3b82f6', 'count', 8, 'total')",
        (exp_id,),
    )
    metric_id = cur.lastrowid
    cur.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period) "
        "VALUES (?, 'Meditation', '#22c55e', 'boolean', 1, 'day')",
        (exp_id,),
    )
    bool_id = cur.lastrowid
    conn.commit()
    conn.close()
    return exp_id, metric_id, bool_id


def _delete_api_experiment(exp_id):
    conn = sqlite3.connect(user_db_path())
    conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
    conn.commit()
    conn.close()


def test_experiments_active_has_metrics(auth_client):
    exp_id, _, _ = _seed_api_experiment()
    try:
        body = auth_client.get("/api/experiments/active", headers=KEY).json()
        exp = next(e for e in body["experiments"] if e["id"] == exp_id)
        assert len(exp["metrics"]) == 2
        gate = next(m for m in exp["metrics"] if m["name"] == "Gate executed")
        for key in ("kind", "color", "target_value", "target_period", "logged_today", "logged_week", "logged_total"):
            assert key in gate
        assert gate["kind"] == "count"
        assert gate["target_value"] == 8
    finally:
        _delete_api_experiment(exp_id)


def test_api_post_entry_requires_key(auth_client):
    exp_id, metric_id, _ = _seed_api_experiment()
    try:
        resp = auth_client.post(f"/api/experiments/{exp_id}/entries", json={"metric": metric_id})
        assert resp.status_code == 401
    finally:
        _delete_api_experiment(exp_id)


def test_api_post_entry_by_name_and_progress(auth_client):
    exp_id, _, _ = _seed_api_experiment()
    try:
        resp = auth_client.post(
            f"/api/experiments/{exp_id}/entries",
            json={"metric": "gate executed", "value": 2, "notes": "10-min walk"},
            headers=KEY,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["kind"] == "count"
        assert body["value"] == 2

        active = auth_client.get("/api/experiments/active", headers=KEY).json()
        exp = next(e for e in active["experiments"] if e["id"] == exp_id)
        gate = next(m for m in exp["metrics"] if m["name"] == "Gate executed")
        assert gate["logged_today"] == 2
        assert gate["logged_total"] == 2
    finally:
        _delete_api_experiment(exp_id)


def test_api_post_entry_boolean_upsert(auth_client):
    exp_id, _, bool_id = _seed_api_experiment()
    try:
        for value in (1, 0):
            resp = auth_client.post(
                f"/api/experiments/{exp_id}/entries",
                json={"metric": bool_id, "value": value},
                headers=KEY,
            )
            assert resp.status_code == 200
        conn = sqlite3.connect(user_db_path())
        rows = conn.execute(
            "SELECT value FROM experiment_entries WHERE experiment_id = ? AND activity_type_id = ?",
            (exp_id, bool_id),
        ).fetchall()
        conn.close()
        assert rows == [(0,)], "boolean via API must upsert to one row, last write wins"
    finally:
        _delete_api_experiment(exp_id)


def test_api_post_entry_validation(auth_client):
    exp_id, metric_id, _ = _seed_api_experiment()
    try:
        # Unknown metric name → 404
        resp = auth_client.post(f"/api/experiments/{exp_id}/entries", json={"metric": "ghost"}, headers=KEY)
        assert resp.status_code == 404
        # Out-of-bounds value → 422
        resp = auth_client.post(
            f"/api/experiments/{exp_id}/entries", json={"metric": metric_id, "value": 100000}, headers=KEY
        )
        assert resp.status_code == 422
        # Bad date → 422
        resp = auth_client.post(
            f"/api/experiments/{exp_id}/entries",
            json={"metric": metric_id, "date": "not-a-date"},
            headers=KEY,
        )
        assert resp.status_code == 422
        # Valid ISO date outside the experiment window → 422 (would be invisible
        # in every grid/progress view — a silent success for the MCP client)
        resp = auth_client.post(
            f"/api/experiments/{exp_id}/entries",
            json={"metric": metric_id, "date": "2020-01-01"},
            headers=KEY,
        )
        assert resp.status_code == 422
        # Unknown experiment → 404
        resp = auth_client.post("/api/experiments/999999/entries", json={"metric": "x"}, headers=KEY)
        assert resp.status_code == 404

        # Inactive experiment → 409
        conn = sqlite3.connect(user_db_path())
        conn.execute("UPDATE experiments SET status = 'completed' WHERE id = ?", (exp_id,))
        conn.commit()
        conn.close()
        resp = auth_client.post(f"/api/experiments/{exp_id}/entries", json={"metric": metric_id}, headers=KEY)
        assert resp.status_code == 409
    finally:
        _delete_api_experiment(exp_id)
