"""Data export completeness, the markdown download, and deployment health."""

import sqlite3

from conftest import csrf_token, user_db_path


def test_json_export_includes_all_user_tables(auth_client):
    resp = auth_client.get("/settings/export/json")
    assert resp.status_code == 200
    data = resp.json()
    # Regression: these were missing, so exports couldn't recreate onboarding
    # context or experiments with week-level targets/summaries.
    for table in (
        "user_profiles",
        "experiment_weeks",
        "experiment_summaries",
        "daily_briefings",
        "exercise_library",
        "app_settings",
        "goal_reps",
    ):
        assert table in data, f"{table} missing from JSON export"
    # Credentials must never be exported.
    assert "llm_providers" not in data
    assert "integrations" not in data


def test_markdown_export_is_a_download_of_the_chosen_sections(auth_client):
    """Nothing is written on the server: the person gets the file."""
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_logs (date, energy, notes) VALUES (date('now', 'localtime'), 7, 'ZZ export note')"
        )
    try:
        token = csrf_token(auth_client, "/settings?tab=data")
        resp = auth_client.post(
            "/settings/export", data={"scope": "weekly", "sections": ["daily_logs"], "_csrf_token": token}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert resp.headers["content-disposition"].startswith('attachment; filename="virgil-weekly-')
        assert "ZZ export note" in resp.text

        refused = auth_client.post(
            "/settings/export",
            data={"scope": "decade", "sections": ["daily_logs"], "_csrf_token": token},
            follow_redirects=False,
        )
        assert refused.status_code == 303
    finally:
        with sqlite3.connect(user_db_path()) as conn:
            conn.execute("DELETE FROM daily_logs WHERE notes = 'ZZ export note'")


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_healthz_degraded_on_migration_failure(client):
    from app.main import app

    original = getattr(app.state, "migration_failures", [])
    app.state.migration_failures = ["broken-user.db"]
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"
    finally:
        app.state.migration_failures = original


def test_healthz_degraded_on_central_migration_failure(client):
    from app.main import app

    original = getattr(app.state, "central_migration_failure", False)
    app.state.central_migration_failure = True
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 503
        assert resp.json() == {"status": "degraded"}

        blocked = client.get("/login")
        assert blocked.status_code == 503
        assert blocked.json() == {"status": "unavailable"}
    finally:
        app.state.central_migration_failure = original
