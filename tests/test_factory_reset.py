"""Factory reset must wipe data but keep a WORKING account.

Regression: the old handler deleted the user DB file, left the session alive,
and redirected to a nonexistent /setup route — the next request then created an
empty un-migrated SQLite file and crashed on missing tables.
"""

import sqlite3
from urllib.parse import parse_qs, urlsplit

from conftest import TEST_PASSWORD, _complete_onboarding, csrf_token, user_db_path


def _backups_of(stem: str) -> list[str]:
    from app.services.backup import BACKUP_DIR

    return sorted(path.name for path in BACKUP_DIR.rglob(f"{stem}-*.db"))


def test_factory_reset_recreates_db_and_restarts_onboarding(auth_client):
    from app.services.backup import BACKUP_DIR

    old_stem = user_db_path().stem
    # A reset moves the account to a new file name, so backups under the old one
    # were orphaned forever. Another account's backup must survive.
    (BACKUP_DIR / "pre-migration").mkdir(parents=True, exist_ok=True)
    kept = BACKUP_DIR / "11111111-1111-4111-8111-111111111111-2026-10-01T0000.db"
    for path in (
        BACKUP_DIR / f"{old_stem}-2026-10-01T0000.db",
        BACKUP_DIR / "pre-migration" / f"{old_stem}-pre-migration-v036.db",
        kept,
    ):
        path.write_bytes(b"")

    # Seed a row so we can prove the wipe actually happened.
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-07-01', 7)")
    conn.commit()
    conn.close()

    token = csrf_token(auth_client, "/settings?tab=security")
    resp = auth_client.post(
        "/settings/factory-reset",
        data={"password": TEST_PASSWORD, "_csrf_token": token},
        follow_redirects=False,
    )
    try:
        assert resp.status_code == 303
        redirect = urlsplit(resp.headers["location"])
        assert redirect.path == "/onboarding"
        assert parse_qs(redirect.query) == {"msg": ["Factory reset completed. Start onboarding again."]}
        assert _backups_of(old_stem) == []
        assert kept.exists()

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
        kept.unlink(missing_ok=True)
        _complete_onboarding()


def test_factory_reset_refuses_without_the_password(auth_client):
    """A session left open on a shared screen must not be enough to wipe an account."""
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT INTO daily_logs (date, energy) VALUES ('2026-07-02', 6)")
    conn.commit()
    try:
        for password in ("", "not-the-password"):
            token = csrf_token(auth_client, "/settings?tab=security")
            resp = auth_client.post(
                "/settings/factory-reset", data={"password": password, "_csrf_token": token}, follow_redirects=False
            )
            assert resp.status_code == 303
            assert parse_qs(urlsplit(resp.headers["location"]).query) == {
                "tab": ["data"],
                "err": ["Password is incorrect. Nothing was reset."],
            }
        rows = conn.execute("SELECT energy FROM daily_logs WHERE date = '2026-07-02'").fetchall()
        assert rows == [(6,)]
    finally:
        conn.execute("DELETE FROM daily_logs WHERE date = '2026-07-02'")
        conn.commit()
        conn.close()
