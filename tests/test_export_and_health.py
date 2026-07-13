"""Data export completeness, per-user export filenames, and deployment health."""

from app.services.markdown_export import valid_export_filename


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
    ):
        assert table in data, f"{table} missing from JSON export"
    # Credentials must never be exported.
    assert "llm_providers" not in data
    assert "integrations" not in data


def test_export_filename_validation():
    assert valid_export_filename("virgil.md")
    assert valid_export_filename("virgil-kb.md")
    assert not valid_export_filename("")
    assert not valid_export_filename(".md")
    assert not valid_export_filename("../evil.md")
    assert not valid_export_filename("dir/evil.md")
    assert not valid_export_filename("dir\\evil.md")
    assert not valid_export_filename(".hidden.md")
    assert not valid_export_filename("notes.txt")
    assert not valid_export_filename("x" * 200 + ".md")


def test_export_filename_save_and_reject(auth_client):
    from conftest import csrf_token

    token = csrf_token(auth_client, "/settings?tab=data")
    resp = auth_client.post(
        "/settings/export/filename",
        data={"export_filename": "../escape.md", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]

    resp = auth_client.post(
        "/settings/export/filename",
        data={"export_filename": "virgil-test.md", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "msg=" in resp.headers["location"]

    page = auth_client.get("/settings?tab=data")
    assert "virgil-test.md" in page.text


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
