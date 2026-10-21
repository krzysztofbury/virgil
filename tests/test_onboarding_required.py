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


def test_onboarding_shows_only_its_own_way_out(auth_client):
    """The app's navigation led to pages that only redirect back here (#95)."""
    page = auth_client.get("/onboarding?step=1").text
    nav = page[page.index('<nav class="top-nav"') : page.index("</nav>")]
    assert 'href="/training"' not in nav and 'href="/settings"' not in nav
    assert 'action="/logout"' in nav
    assert 'class="bottom-bar"' not in page


def test_step_one_no_longer_asks_habits_or_clears_them(auth_client):
    """Habits are asked in step 4. Saving step 1 must keep what an earlier
    version stored in its own habit fields: the onboarding context reads them."""
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute("INSERT OR IGNORE INTO user_profiles (id) VALUES (1)")
        conn.execute("UPDATE user_profiles SET habits_good = 'ZZ reading', habits_bad = 'ZZ scrolling' WHERE id = 1")
    assert 'name="habits_good"' not in auth_client.get("/onboarding?step=1").text
    token = csrf_token(auth_client, "/onboarding?step=1")
    saved = auth_client.post(
        "/onboarding/step/1", data={"_csrf_token": token, "age": "40", "family": ""}, follow_redirects=False
    )
    assert saved.status_code == 303
    with sqlite3.connect(user_db_path()) as conn:
        kept = conn.execute("SELECT habits_good, habits_bad FROM user_profiles WHERE id = 1").fetchone()
    assert kept == ("ZZ reading", "ZZ scrolling")


def test_medical_step_without_a_key_offers_next_not_an_upload(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "")
    page = auth_client.get("/onboarding?step=5").text
    assert 'type="file"' not in page, "an upload the server would refuse must not be offered"
    assert 'href="/onboarding?step=6" class="btn btn-primary">Next' in page

    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "test-key")
    page = auth_client.get("/onboarding?step=5").text
    assert 'type="file"' in page
    assert "Skip this step" in page
