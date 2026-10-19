"""Freedom: habits being broken, each with one day log (clean / slipped, plus the
amount and near miss the habit asks for) and bricks (urges survived: hook, craving,
story) on one page with one timeline. A day-based streak cannot see a near miss,
hence the day log. The weekly 75% clean rate (Gola) never resets to 0.

The Porn habit carries the old No Porn module's contract: a slip ("I watched")
needs at least 5 minutes, and edging is its near miss.
"""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, user_db_path


def _fetchall(sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _execute(sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _enable(client, *slugs: str) -> dict[str, int]:
    """Freedom on, the given catalog habits tracked; returns their ids."""
    _execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_freedom', '1')")
    token = csrf_token(client, "/freedom")
    for slug in slugs or ("porn",):
        client.post("/freedom/habits", data={"slug": slug, "_csrf_token": token}, follow_redirects=False)
    return dict(_fetchall("SELECT slug, id FROM habits"))


def _day(client, slug: str, **data):
    token = csrf_token(client, f"/freedom?habit={slug}")
    return client.post(f"/freedom/{slug}/daily", data={**data, "_csrf_token": token}, follow_redirects=False)


def _brick(client, slug: str, **data):
    token = csrf_token(client, f"/freedom?habit={slug}")
    return client.post(f"/freedom/{slug}/bricks", data={**data, "_csrf_token": token}, follow_redirects=False)


def test_daily_log_upserts_one_row_per_habit_and_date(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-08-20"

    assert _day(auth_client, "porn", date=day, slipped="1", amount="45", near_miss="1", note="stres").status_code == 303
    assert _day(auth_client, "porn", date=day, slipped="1", amount="90", note="nuda").status_code == 303

    rows = _fetchall(
        "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
    )
    assert rows == [(1, 90, 0, "nuda")], "second save must update the same row; near miss unchecked -> 0"


def test_a_slip_records_one_relapse_marker(auth_client):
    """A slip feeds the streak and week-clean machinery (pmo_events), but saving the
    same day twice must not double-count the relapse."""
    porn = _enable(auth_client)["porn"]
    day = "2026-08-19"

    for _ in range(2):
        _day(auth_client, "porn", date=day, slipped="1", amount="5")

    rows = _fetchall(
        "SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND event_type = 'relapse' AND date = ?", (porn, day)
    )
    assert rows[0][0] == 1


def test_a_clean_day_records_no_relapse(auth_client):
    porn = _enable(auth_client)["porn"]
    day = "2026-08-18"

    _day(auth_client, "porn", date=day, slipped="0")

    assert _fetchall("SELECT slipped FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)) == [(0,)]
    assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day))[0][0] == 0


def test_correcting_a_slip_to_clean_removes_its_marker(auth_client):
    """A misclicked slip is a normal correction: re-saving the day as clean must remove
    the relapse the day log itself created, or the streak contradicts the log for good."""
    porn = _enable(auth_client)["porn"]
    day = "2026-08-17"

    for slipped in ("1", "0"):
        _day(auth_client, "porn", date=day, slipped=slipped, amount="5")

    assert _fetchall("SELECT slipped FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)) == [(0,)]
    assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day))[0][0] == 0


def test_correction_keeps_a_relapse_recorded_another_way(auth_client):
    """Correcting the day log to clean deletes only its own marker, never a relapse
    recorded by another path (those carry their own notes)."""
    porn = _enable(auth_client)["porn"]
    day = "2026-08-16"

    _execute(
        "INSERT INTO pmo_events (habit_id, date, event_type, notes) VALUES (?, ?, 'relapse', 'recorded by hand')",
        (porn, day),
    )
    for slipped in ("1", "0"):
        _day(auth_client, "porn", date=day, slipped=slipped, amount="5")

    assert _fetchall("SELECT notes FROM pmo_events WHERE habit_id = ? AND date = ?", (porn, day)) == [
        ("recorded by hand",)
    ]


def test_a_porn_slip_needs_at_least_five_minutes(auth_client):
    porn = _enable(auth_client)["porn"]
    day = (date.today() - timedelta(days=5)).isoformat()

    for amount in ("", "4"):
        resp = _day(auth_client, "porn", date=day, slipped="1", amount=amount)
        assert "err=" in resp.headers["location"]

    assert _fetchall("SELECT id FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)) == []


def test_a_clean_day_clears_slip_values_but_keeps_the_note(auth_client):
    porn = _enable(auth_client)["porn"]
    day = (date.today() - timedelta(days=4)).isoformat()

    _day(auth_client, "porn", date=day, slipped="1", amount="12", near_miss="1", note="slip record")
    _day(
        auth_client,
        "porn",
        date=day,
        slipped="0",
        amount="12",
        near_miss="1",
        note="brief accidental exposure under five minutes",
    )

    assert _fetchall(
        "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?", (porn, day)
    ) == [(0, None, 0, "brief accidental exposure under five minutes")]


def test_habits_keep_separate_days_and_streaks(auth_client):
    """A slip on one habit is not a slip on another, even on the same date."""
    ids = _enable(auth_client, "porn", "alcohol")
    day = (date.today() - timedelta(days=1)).isoformat()

    _day(auth_client, "alcohol", date=day, slipped="1", amount="2")
    _day(auth_client, "porn", date=day, slipped="0")

    assert _fetchall("SELECT slipped FROM feniks_daily WHERE habit_id = ? AND date = ?", (ids["alcohol"], day)) == [
        (1,)
    ]
    assert _fetchall("SELECT slipped FROM feniks_daily WHERE habit_id = ? AND date = ?", (ids["porn"], day)) == [(0,)]
    assert _fetchall("SELECT COUNT(*) FROM pmo_events WHERE habit_id = ? AND date = ?", (ids["porn"], day))[0][0] == 0

    html = auth_client.get("/freedom").text
    assert "Today" in html and "Alcohol" in html and "Porn" in html, "two habits list each at a glance"


def test_a_habit_without_a_unit_slips_without_an_amount(auth_client):
    token = csrf_token(auth_client, "/freedom")
    _enable(auth_client)
    auth_client.post("/freedom/habits", data={"name": "Energy drinks", "_csrf_token": token}, follow_redirects=False)
    day = (date.today() - timedelta(days=2)).isoformat()

    resp = _day(auth_client, "energy-drinks", date=day, slipped="1")

    assert "err=" not in resp.headers["location"]
    assert _fetchall(
        "SELECT d.slipped, d.amount FROM feniks_daily d JOIN habits h ON h.id = d.habit_id "
        "WHERE h.slug = 'energy-drinks' AND d.date = ?",
        (day,),
    ) == [(1, None)]


def test_brick_create_and_render(auth_client):
    porn = _enable(auth_client)["porn"]

    resp = _brick(
        auth_client,
        "porn",
        date=date.today().isoformat(),
        hook="Poszedłem na spacer",
        craving="8",
        story="sam w domu po pracy; zamknąłem laptopa i wyszedłem, głód minął po 20 minutach",
    )
    assert resp.status_code == 303

    assert ("Poszedłem na spacer", 8) in _fetchall(
        "SELECT hook, craving FROM feniks_bricks WHERE habit_id = ?", (porn,)
    )
    assert "Poszedłem na spacer" in auth_client.get("/freedom?habit=porn").text


def test_brick_requires_hook(auth_client):
    """A brick without its memory hook is not a brick (Gola: hak pamięciowy)."""
    _enable(auth_client)
    before = _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0]

    _brick(auth_client, "porn", date=date.today().isoformat(), hook="  ")

    assert _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0] == before


def test_brick_form_offers_the_viewed_date_and_records_it(auth_client):
    """A brick laid while reviewing an earlier day belongs to that day, not today."""
    _enable(auth_client)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    html = auth_client.get(f"/freedom?habit=porn&date={yesterday}").text
    assert f'<input type="date" name="date" value="{yesterday}" max="{date.today().isoformat()}"' in html
    assert f'data-draft-key="freedom-brick:porn:{yesterday}"' in html

    resp = _brick(auth_client, "porn", date=yesterday, hook="brick dated by hand")

    assert resp.status_code == 303
    assert resp.headers["location"].startswith(f"/freedom?habit=porn&date={yesterday}")
    assert _fetchall("SELECT date FROM feniks_bricks WHERE hook = ?", ("brick dated by hand",)) == [(yesterday,)]


def test_brick_refuses_a_future_or_unparseable_date(auth_client):
    _enable(auth_client)
    before = _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0]

    for bad in ((date.today() + timedelta(days=1)).isoformat(), "not-a-date"):
        resp = _brick(auth_client, "porn", date=bad, hook="should not land")
        assert resp.status_code == 303
        assert "err=" in resp.headers["location"]

    assert _fetchall("SELECT COUNT(*) FROM feniks_bricks")[0][0] == before


def test_brick_normalises_a_compact_iso_date(auth_client):
    """date.fromisoformat also accepts YYYYMMDD, which no longer sorts as text."""
    _enable(auth_client)
    day = date.today() - timedelta(days=3)

    _brick(auth_client, "porn", date=day.strftime("%Y%m%d"), hook="compact date")

    assert _fetchall("SELECT date FROM feniks_bricks WHERE hook = ?", ("compact date",)) == [(day.isoformat(),)]


def test_brick_can_be_corrected_and_removed(auth_client):
    _enable(auth_client)
    day = (date.today() - timedelta(days=2)).isoformat()
    _brick(auth_client, "porn", date=day, hook="before correction", craving="9")
    brick_id = _fetchall("SELECT id FROM feniks_bricks WHERE hook = 'before correction'")[0][0]
    token = csrf_token(auth_client, "/freedom")

    updated = auth_client.post(
        f"/freedom/bricks/{brick_id}/edit",
        data={
            "date": day,
            "hook": "after correction",
            "craving": "6",
            "story": "corrected detail",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert updated.status_code == 303
    assert _fetchall("SELECT hook, craving, story FROM feniks_bricks WHERE id = ?", (brick_id,)) == [
        ("after correction", 6, "corrected detail")
    ]

    removed = auth_client.post(
        f"/freedom/bricks/{brick_id}/delete", data={"_csrf_token": token}, follow_redirects=False
    )
    assert removed.status_code == 303
    assert _fetchall("SELECT id FROM feniks_bricks WHERE id = ?", (brick_id,)) == []


def test_page_is_single_flow_in_the_habits_own_words(auth_client):
    """One page, one decision per habit: the day choice in the habit's words, brick
    capture and one timeline. Retired No Porn elements stay gone."""
    _enable(auth_client)
    html = auth_client.get("/freedom?habit=porn").text
    assert "Clean day" in html, "day choice must be explicit and human"
    assert "I watched" in html, "the Porn habit's own slip label"
    assert 'name="near_miss"' in html and "Edging" in html
    assert 'name="amount"' in html and "Minutes" in html
    assert "at least 5 total minutes" in html, "the slip definition sits beside the question"
    assert "one distinct decision episode" in html
    assert "This week:" in html, "Gola weekly clean-rate stays"
    assert "Target 75%" in html
    for retired in ("Journal Entry", "Two Pleasures", "Report relapse", "Streak Progress", "Milestones"):
        assert retired not in html


def test_a_clean_day_note_is_visible_in_the_timeline(auth_client):
    porn = _enable(auth_client)["porn"]
    _execute(
        "INSERT OR REPLACE INTO feniks_daily(habit_id, date, slipped, note) VALUES(?, '2026-08-30', 0, ?)",
        (porn, "clean measurement remains visible"),
    )
    assert "clean measurement remains visible" in auth_client.get("/freedom?habit=porn").text


def test_the_old_address_redirects(auth_client):
    _enable(auth_client)
    resp = auth_client.get("/feniks?date=2026-08-01", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/freedom?date=2026-08-01"


def test_untracking_keeps_history_and_tracking_again_restores_it(auth_client):
    ids = _enable(auth_client, "porn", "alcohol")
    day = (date.today() - timedelta(days=6)).isoformat()
    _day(auth_client, "alcohol", date=day, slipped="0", note="kept through untracking")
    token = csrf_token(auth_client, "/freedom")

    auth_client.post("/freedom/alcohol/untrack", data={"_csrf_token": token}, follow_redirects=False)
    assert "kept through untracking" not in auth_client.get("/freedom").text
    assert _fetchall("SELECT note FROM feniks_daily WHERE habit_id = ? AND date = ?", (ids["alcohol"], day)) == [
        ("kept through untracking",)
    ]

    auth_client.post("/freedom/habits", data={"slug": "alcohol", "_csrf_token": token}, follow_redirects=False)
    assert "kept through untracking" in auth_client.get("/freedom?habit=alcohol").text


def test_settings_checklist_sets_the_tracked_habits(auth_client):
    _enable(auth_client, "porn")
    token = csrf_token(auth_client, "/settings")

    auth_client.post(
        "/settings/features",
        data={
            "feature_freedom": "on",
            "freedom_habits_shown": "1",
            "habits": ["porn", "gambling"],
            "custom_habit": "Doomscrolling news",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )

    tracked = {row[0] for row in _fetchall("SELECT slug FROM habits WHERE tracked = 1")}
    assert tracked == {"porn", "gambling", "doomscrolling-news"}


def test_custom_habits_with_non_latin_names_stay_apart(auth_client):
    """Slugs kept only [a-z0-9], so every Cyrillic name became "habit" and a second
    one resumed the first, merging its days, bricks and streak into the first's."""
    token = csrf_token(auth_client, "/freedom")
    names = ["Алкоголь", "Сахар", "АЛКОГОЛЬ", "Słodycze"]
    try:
        for name in names:
            resp = auth_client.post(
                "/freedom/habits", data={"name": name, "_csrf_token": token}, follow_redirects=False
            )
            assert resp.status_code == 303

        rows = _fetchall(
            "SELECT slug, name FROM habits WHERE name IN (?, ?, ?) ORDER BY id", ("Алкоголь", "Сахар", "Słodycze")
        )
        # The same name in other case resumes the first habit instead of adding one.
        assert [tuple(row) for row in rows] == [("habit", "Алкоголь"), ("habit-2", "Сахар"), ("slodycze", "Słodycze")]
    finally:
        _execute("DELETE FROM habits WHERE slug IN ('habit', 'habit-2', 'slodycze')")


def test_api_noporn_reads_the_porn_habit(auth_client, monkeypatch):
    _enable(auth_client)
    _brick(auth_client, "porn", date=date.today().isoformat(), hook="api-test-brick")
    monkeypatch.setattr("app.config.API_SENSITIVE", True)

    resp = auth_client.get("/api/noporn", headers={"X-API-Key": "test-key-123"})

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["daily"], list)
    assert body["bricks_total"] >= 1
    brick = next(b for b in body["bricks"] if b["hook"] == "api-test-brick")
    assert "story" in brick and "craving" in brick


def test_a_quick_check_off_keeps_the_habit_on_screen(auth_client):
    _enable(auth_client, "porn", "alcohol")
    resp = _day(auth_client, "alcohol", date=date.today().isoformat(), slipped="0", view="porn")
    assert resp.headers["location"].startswith("/freedom?habit=porn")


def test_dashboard_checks_a_habit_off_in_place_without_overwriting_a_logged_day(auth_client):
    ids = _enable(auth_client, "porn", "alcohol")
    today = date.today().isoformat()
    _execute("DELETE FROM feniks_daily WHERE date = ?", (today,))  # other tests check today off too
    try:
        page = auth_client.get("/").text
        assert 'id="freedom-today-block"' in page
        assert 'hx-post="/freedom/alcohol/clean-today"' in page

        token = csrf_token(auth_client, "/")
        headers = {"X-CSRF-Token": token, "HX-Request": "true"}
        tapped = auth_client.post("/freedom/alcohol/clean-today", headers=headers)
        assert tapped.status_code == 200
        assert 'id="freedom-today-block"' in tapped.text, "the block is redrawn in place"
        assert 'hx-post="/freedom/alcohol/clean-today"' not in tapped.text, "a logged habit shows its outcome"
        assert _fetchall(
            "SELECT slipped FROM feniks_daily WHERE habit_id = ? AND date = ?", (ids["alcohol"], today)
        ) == [(0,)]

        # A slip logged elsewhere survives a stale Clean tap.
        assert _day(auth_client, "porn", date=today, slipped="1", amount="20").status_code == 303
        auth_client.post("/freedom/porn/clean-today", headers=headers)
        assert _fetchall(
            "SELECT slipped, amount FROM feniks_daily WHERE habit_id = ? AND date = ?", (ids["porn"], today)
        ) == [(1, 20)]
    finally:
        _execute("DELETE FROM feniks_daily WHERE date = ?", (today,))
        _execute("DELETE FROM pmo_events WHERE date = ?", (today,))


def test_dashboard_has_no_freedom_block_when_the_module_is_off(auth_client):
    _execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_freedom', '0')")
    assert 'id="freedom-today-block"' not in auth_client.get("/").text
