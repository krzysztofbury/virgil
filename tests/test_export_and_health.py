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


def test_export_filename_defaults_are_per_user(tmp_path, monkeypatch):
    """The primary user keeps legacy virgil.md; others get unique defaults so
    scheduled exports in the shared second-brain dir can't collide."""
    import asyncio

    async def scenario():
        import aiosqlite

        from app.migrations.runner import run_migrations
        from app.services.markdown_export import export_filename_for

        async def fake_primary():
            return "primary-user-id"

        monkeypatch.setattr("app.central_db.get_primary_user_id", fake_primary)

        db = await aiosqlite.connect(tmp_path / "exp.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        primary = await export_filename_for(db, "primary-user-id")
        secondary = await export_filename_for(db, "aabbccdd-2222-3333-4444-555566667777")
        await db.close()
        return primary, secondary

    primary, secondary = asyncio.run(scenario())
    assert primary == "virgil.md"
    assert secondary == "virgil-aabbccdd.md"
    assert primary != secondary


def test_export_filename_shown_in_data_tab(auth_client):
    """Filename is derived (never user-chosen) and displayed read-only."""
    page = auth_client.get("/settings?tab=data")
    assert page.status_code == 200
    assert "virgil.md" in page.text  # the single test user is the primary account


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
