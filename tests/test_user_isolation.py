"""Two accounts see only their own data, over HTTP.

The old isolation test wrote into one database file and read the other, so it
could not fail: pointing the auth middleware at the first account's database
for everyone still passed it. Here a second account writes through the app and
the first account reads through the app.
"""

import os
import sqlite3
from datetime import date

from conftest import csrf_token, user_db_path
from fastapi.testclient import TestClient

# A day no other test logs. Freedom days use today: a habit tracked today
# refuses days before its start.
DAY = "2024-02-29"
TODAY = date.today().isoformat()
PROBE = "isolation probe written by the second account"


def _central():
    return sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])


def _sign_up_second_account(client) -> str:
    token = csrf_token(client, "/signup")
    response = client.post(
        "/signup",
        data={
            "email": "second@example.com",
            "password": "second-password-123",  # pragma: allowlist secret - synthetic fixture
            "password_confirm": "second-password-123",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    central = _central()
    try:
        return central.execute("SELECT db_filename FROM users WHERE email = 'second@example.com'").fetchone()[0]
    finally:
        central.close()


def test_one_accounts_writes_never_reach_another_account(auth_client, monkeypatch):
    import app.routers.auth as auth_routes
    from app.main import app

    monkeypatch.setattr(auth_routes, "REGISTRATION_OPEN", True)
    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    second = TestClient(app)
    second.portal = auth_client.portal
    second_db = None
    # Read before the second database exists: user_db_path() expects exactly one.
    conn = sqlite3.connect(user_db_path())
    first_tracked_alcohol = bool(conn.execute("SELECT 1 FROM habits WHERE slug = 'alcohol' AND tracked = 1").fetchone())
    conn.close()
    try:
        second_db = _sign_up_second_account(second)
        # Setup, not behavior under test: skip the second account's onboarding.
        conn = sqlite3.connect(os.path.join(users_dir, second_db))
        conn.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '1')",
            [("onboarding_completed",), ("feature_freedom",)],
        )
        conn.commit()
        conn.close()

        token = csrf_token(second, "/daily")
        assert second.post("/daily/save", data={"date": DAY, "notes": PROBE, "_csrf_token": token}).status_code == 200
        second.post("/freedom/habits", data={"slug": "alcohol", "_csrf_token": token})
        second.post("/freedom/alcohol/daily", data={"date": TODAY, "slipped": "0", "note": PROBE, "_csrf_token": token})
        # Control: the writes landed, so their absence below means isolation.
        assert PROBE in second.get(f"/daily/{DAY}").text
        assert PROBE in second.get(f"/freedom?habit=alcohol&date={TODAY}").text

        token = csrf_token(auth_client, "/freedom")
        auth_client.post("/freedom/habits", data={"slug": "alcohol", "_csrf_token": token})
        assert PROBE not in auth_client.get(f"/daily/{DAY}").text
        assert PROBE not in auth_client.get(f"/freedom?habit=alcohol&date={TODAY}").text
        assert auth_client.get(f"/api/daily/{DAY}", headers={"X-API-Key": "test-key-123"}).status_code == 404
    finally:
        second.close()
        if not first_tracked_alcohol:
            token = csrf_token(auth_client, "/freedom")
            auth_client.post("/freedom/alcohol/untrack", data={"_csrf_token": token})
        central = _central()
        central.execute("DELETE FROM users WHERE email = 'second@example.com'")
        central.commit()
        central.close()
        for suffix in ("", "-wal", "-shm"):
            if second_db and os.path.exists(os.path.join(users_dir, second_db + suffix)):
                os.remove(os.path.join(users_dir, second_db + suffix))
