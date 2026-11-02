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


def test_the_summary_lists_every_section_even_when_one_is_empty(auth_client):
    """An empty Summary card gave a new user nothing to review or fix (#163).
    Each section shows, with its Edit link, filled in or not."""
    page = auth_client.get("/onboarding?step=6").text
    summary = page[page.index("<h2") : page.index('href="/onboarding?step=5"')]
    for step, title in [(1, "About You"), (2, "Your Ideal Day"), (3, "End Goals"), (4, "Habits & Training")]:
        assert title in summary, title
        assert f'href="/onboarding?step={step}"' in summary, step


def test_a_step_moves_on_without_a_confirmation_over_next(auth_client):
    """The progress bar shows the step was saved; a toast sat on the Next button (#200)."""
    token = csrf_token(auth_client, "/onboarding?step=1")
    moved = auth_client.post("/onboarding/step/1", data={"_csrf_token": token})
    assert "feedback-success" not in moved.text and "Your Ideal Day" in moved.text
    assert "feedback-error" in auth_client.get("/onboarding?step=2&err=Could+not+save").text


def test_the_summary_lists_the_habits_it_was_told(auth_client):
    token = csrf_token(auth_client, "/onboarding?step=4")
    auth_client.post(
        "/onboarding/step/4",
        data={"habits_build": "Morning walk", "habits_break": "Late-night screens", "_csrf_token": token},
    )
    page = auth_client.get("/onboarding?step=6").text
    assert "Habits you want to build" in page and "Morning walk" in page
    assert "Habits you want to break" in page and "Late-night screens" in page
    auth_client.post(
        "/onboarding/step/4",
        data={"habits_build": "", "habits_break": "", "training_routine": "Three runs a week", "_csrf_token": token},
    )
    page = auth_client.get("/onboarding?step=6").text
    assert "Three runs a week" in page
    section = page[page.index("Habits you want to build") :]
    assert section[: section.index("Three runs a week")].count("Not filled in yet.") == 2
