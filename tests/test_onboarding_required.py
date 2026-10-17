"""Every account goes through onboarding once: there is no way around it."""

import sqlite3

from conftest import csrf_token, user_db_path


def _set_onboarded(value: str) -> None:
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('onboarding_completed', ?)", (value,))


def test_onboarding_cannot_be_skipped_but_can_finish_without_ai(auth_client, monkeypatch):
    # With a key, a normal confirm would queue the paid extras; without_ai must not.
    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "test-key")
    _set_onboarded("0")
    try:
        assert auth_client.get("/", follow_redirects=False).headers["location"] == "/onboarding"
        page = auth_client.get("/onboarding?step=6").text
        assert "Skip onboarding" not in page
        token = csrf_token(auth_client, "/onboarding?step=6")
        assert auth_client.post("/onboarding/skip", data={"_csrf_token": token}).status_code in (404, 405)
        assert auth_client.get("/", follow_redirects=False).headers["location"] == "/onboarding"

        done = auth_client.post(
            "/onboarding/confirm", data={"without_ai": "1", "_csrf_token": token}, follow_redirects=False
        )
        assert done.status_code == 303
        with sqlite3.connect(user_db_path()) as conn:
            completed = conn.execute("SELECT value FROM app_settings WHERE key = 'onboarding_completed'").fetchone()
            paid = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'onboarding_enrichment'").fetchone()
        assert completed == ("1",)
        assert paid == (0,), "finishing without AI buys nothing"
        assert auth_client.get("/", follow_redirects=False).status_code == 200
    finally:
        _set_onboarded("1")
