"""Factory reset must wipe data but keep a WORKING account.

Regression: the old handler deleted the user DB file, left the session alive,
and redirected to a nonexistent /setup route — the next request then created an
empty un-migrated SQLite file and crashed on missing tables.
"""

import sqlite3

from conftest import _complete_onboarding, csrf_token, user_db_path


def test_factory_reset_recreates_db_and_restarts_onboarding(auth_client):
    # Seed a row so we can prove the wipe actually happened.
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-07-01', 7)")
    conn.commit()
    conn.close()

    token = csrf_token(auth_client, "/settings?tab=security")
    resp = auth_client.post(
        "/settings/factory-reset",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/onboarding"

    try:
        # The recreated DB must be fully migrated (schema_migrations populated,
        # seeded data present) and the seeded daily_log gone.
        conn = sqlite3.connect(user_db_path())
        try:
            versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()]
            assert versions, "Recreated DB was not migrated"
            logs = conn.execute("SELECT COUNT(*) FROM daily_logs").fetchone()[0]
            assert logs == 0
            onboarding = conn.execute("SELECT value FROM app_settings WHERE key = 'onboarding_completed'").fetchone()
            assert onboarding is not None
            assert onboarding[0] == "0"
        finally:
            conn.close()

        # Session survives; the app forces onboarding instead of crashing.
        home = auth_client.get("/", follow_redirects=False)
        assert home.status_code == 303
        assert home.headers["location"] == "/onboarding"

        onboarding_page = auth_client.get("/onboarding")
        assert onboarding_page.status_code == 200
    finally:
        # Restore state for the rest of the (session-scoped) suite.
        _complete_onboarding()
