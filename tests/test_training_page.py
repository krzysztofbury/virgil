"""The training page is free-text capture plus read-only history — nothing else.

Replaces the previous suites for this page (a protocol CRUD table, a per-set log
form, an exercise picker and a rest timer). Those surfaces are gone, so tests
that drove them were deleted rather than adapted; what survives here is the
contract that replaced them.

Every "X is absent" assertion is paired with a positive one on the same
response. An absence check alone passes just as happily when the page 500s or
renders empty, which is the failure it is supposed to catch.
"""

import sqlite3
from datetime import date
from pathlib import Path

from conftest import csrf_token, user_db_path


def _page(auth_client) -> str:
    resp = auth_client.get("/training")
    assert resp.status_code == 200, f"training page must render, got {resp.status_code}"
    return resp.text


def test_capture_form_is_the_only_input(auth_client):
    html = _page(auth_client)

    assert 'action="/training/wod"' in html, "the capture form is the page's one input"
    assert 'name="wod_text"' in html

    assert "Training Protocol" not in html, "the protocol table was removed"
    assert "Log Workout" not in html, "the per-set log form was removed"
    assert "Rest Timer" not in html, "the rest timer was removed"
    assert 'action="/training/exercise"' not in html, "protocol CRUD was removed"


def test_kpis_and_history_survive(auth_client):
    """Creates its own session rather than relying on one being there.

    `auth_client` is session-scoped and the DB is shared across test files, so
    the delete-form assertion below passed only because an earlier file happened
    to leave a row behind — green in a full run, red when this file runs alone.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 42, 'ZZTestPageSession')",
            # Today, not a hardcoded date: the default calendar shows the
            # current year, so a fixed past date would require year navigation.
            (date.today().isoformat(),),
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    try:
        html = auth_client.get(f"/training?day={date.today().isoformat()}").text
        assert "This Week" in html
        assert "Workout History" in html
        assert f'action="/training/session/{session_id}/delete"' in html, "history keeps its per-session delete"
    finally:
        conn = sqlite3.connect(user_db_path())
        try:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def test_history_calendar_groups_sessions_by_day(auth_client):
    conn = sqlite3.connect(user_db_path())
    session_ids = []
    try:
        for note in ("ZZ calendar morning", "ZZ calendar evening"):
            cur = conn.execute(
                "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES ('2014-04-05', 45, ?)",
                (note,),
            )
            session_ids.append(cur.lastrowid)
        conn.commit()

        html = auth_client.get("/training?year=2014").text
        assert html.count('data-training-date="2014-04-05"') == 1
        assert 'data-session-count="2"' in html
        assert 'class="year-cal-dot training-cal-dot training-cal-level-3"' in html, (
            "90 total minutes must use the 90+ intensity"
        )
        assert "/training?year=2014&amp;day=2014-04-05#training-day-2014-04-05" in html
        assert "ZZ calendar morning" not in html, "annual summaries must not eagerly render session details"
        assert 'id="training-day-2014-04-05"' not in html

        fallback_html = auth_client.get("/training?year=2014&day=2014-04-05").text
        assert "ZZ calendar morning" in fallback_html
        assert "ZZ calendar evening" in fallback_html
        assert 'id="training-day-2014-04-05"' in fallback_html
        assert 'aria-labelledby="training-day-title-2014-04-05" open' in fallback_html, (
            "the day detail must open through its server URL without JavaScript"
        )
    finally:
        conn.executemany("DELETE FROM training_sessions WHERE id = ?", [(value,) for value in session_ids])
        conn.commit()
        conn.close()


def test_selected_day_session_details_are_paginated(auth_client):
    from app.routers.training import MAX_DAY_SESSIONS

    conn = sqlite3.connect(user_db_path())
    session_ids = []
    try:
        for number in range(MAX_DAY_SESSIONS + 1):
            cur = conn.execute(
                "INSERT INTO training_sessions (date, notes) VALUES ('2013-03-03', ?)",
                (f"ZZ daily page {number}",),
            )
            session_ids.append(cur.lastrowid)
        conn.commit()

        first = auth_client.get("/training?year=2013&day=2013-03-03").text
        assert first.count('data-training-session="') == MAX_DAY_SESSIONS
        assert "ZZ daily page 20" in first
        assert "ZZ daily page 0" not in first
        assert f"before_id={session_ids[1]}" in first

        second = auth_client.get(f"/training?year=2013&day=2013-03-03&before_id={session_ids[1]}").text
        assert second.count('data-training-session="') == 1
        assert "ZZ daily page 0" in second
        assert f"after_id={session_ids[0]}" in second
        assert "before_id=" not in second, "the terminal page must not link past the oldest session"

        newer = auth_client.get(f"/training?year=2013&day=2013-03-03&after_id={session_ids[0]}").text
        assert "ZZ daily page 20" in newer
        assert "ZZ daily page 0" not in newer

        stale = auth_client.get(f"/training?year=2013&day=2013-03-03&before_id={session_ids[0]}")
        assert stale.status_code == 404
        assert auth_client.get("/training?year=2013&day=2013-03-03&before_id=bad").status_code == 422
        assert auth_client.get(f"/training?year=2013&before_id={session_ids[1]}").status_code == 422
        assert (
            auth_client.get(
                f"/training?year=2013&day=2013-03-03&before_id={session_ids[1]}&after_id={session_ids[0]}"
            ).status_code
            == 422
        )
    finally:
        conn.executemany("DELETE FROM training_sessions WHERE id = ?", [(value,) for value in session_ids])
        conn.commit()
        conn.close()


def test_personal_bests_group_rep_records_by_movement(auth_client):
    conn = sqlite3.connect(user_db_path())
    exercise_id = session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('ZZ grouped PB', 'Core', 'reps', 1)"
        )
        exercise_id = cur.lastrowid
        cur = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (date.today().isoformat(),))
        session_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, ?, ?)",
            [
                (session_id, exercise_id, 1, 3, 80.0),
                (session_id, exercise_id, 2, 5, 70.0),
                (session_id, exercise_id, 3, 5, 75.0),
            ],
        )
        conn.commit()

        html = _page(auth_client)
        assert html.count('data-pb-exercise="ZZ grouped PB"') == 1
        assert 'data-pb-exercise="ZZ grouped PB" data-pb-best="80.0"' in html
        assert 'data-pb-effort="3" data-pb-weight="80.0"' in html
        assert 'data-pb-effort="5" data-pb-weight="75.0"' in html
        assert 'data-pb-effort="5" data-pb-weight="70.0"' not in html
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if exercise_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
        conn.commit()
        conn.close()


def test_selected_day_bounds_entries_per_session_without_hiding_recovery(auth_client):
    from app.routers.training import MAX_CONFIRM_ENTRIES

    conn = sqlite3.connect(user_db_path())
    exercise_id = full_session_id = stranded_session_id = None
    try:
        exercise_id = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('ZZ entry bound', 'Core', 'reps', 1)"
        ).lastrowid
        full_session_id = conn.execute(
            "INSERT INTO training_sessions (date, notes) VALUES ('2012-02-02', 'ZZ full session')"
        ).lastrowid
        stranded_session_id = conn.execute(
            "INSERT INTO training_sessions (date, notes) VALUES ('2012-02-02', 'ZZ recover me')"
        ).lastrowid
        conn.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps) VALUES (?, ?, ?, 1)",
            [(full_session_id, exercise_id, number) for number in range(1, MAX_CONFIRM_ENTRIES + 2)],
        )
        conn.commit()

        html = auth_client.get("/training?year=2012&day=2012-02-02").text
        assert f"Showing the first {MAX_CONFIRM_ENTRIES} of {MAX_CONFIRM_ENTRIES + 1} sets." in html
        assert f'action="/training/session/{stranded_session_id}/manual"' in html
        assert "ZZ full session" in html
        assert "Stored entries could not be rendered" not in html
    finally:
        if full_session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (full_session_id,))
        for session_id in (full_session_id, stranded_session_id):
            if session_id is not None:
                conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if exercise_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
        conn.commit()
        conn.close()


def test_personal_best_efforts_are_bounded_and_disclosed(auth_client):
    from app.routers.training import MAX_PB_RECORDS_PER_MOVEMENT

    conn = sqlite3.connect(user_db_path())
    exercise_id = session_id = None
    try:
        exercise_id = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('ZZ bounded PB', 'Core', 'reps', 1)"
        ).lastrowid
        session_id = conn.execute(
            "INSERT INTO training_sessions (date) VALUES (?)", (date.today().isoformat(),)
        ).lastrowid
        conn.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, ?, ?)",
            [
                (session_id, exercise_id, effort, effort, float(effort))
                for effort in range(1, MAX_PB_RECORDS_PER_MOVEMENT + 2)
            ],
        )
        conn.commit()

        html = _page(auth_client)
        block = html.split('data-pb-exercise="ZZ bounded PB"', 1)[1].split("</article>", 1)[0]
        assert block.count("data-pb-effort=") == MAX_PB_RECORDS_PER_MOVEMENT
        assert f'data-pb-effort="{MAX_PB_RECORDS_PER_MOVEMENT + 1}"' not in block
        assert "Showing a bounded recent selection of Personal Best records." in html
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if exercise_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
        conn.commit()
        conn.close()


def test_native_movement_picker_uses_theme_aware_popup_colors():
    css = (Path(__file__).parents[1] / "app/static/css/app.css").read_text()
    assert "select option, select optgroup" in css
    assert "background-color: var(--bg-dropdown)" in css
    assert "color-scheme: dark" in css
    assert "color-scheme: light" in css


def test_global_csrf_injector_excludes_navigation_forms():
    base = (Path(__file__).parents[1] / "app/templates/base.html").read_text()
    assert "form.method.toUpperCase() === 'POST'" in base
    assert "if (mutates && !form.querySelector" in base
    assert "form.hasAttribute('hx-post')" in base, "HTMX mutation forms without method=POST still need a token"


def test_timed_personal_bests_group_by_duration(auth_client):
    conn = sqlite3.connect(user_db_path())
    exercise_id = session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('ZZ timed PB', 'Core', 'time', 1)"
        )
        exercise_id = cur.lastrowid
        cur = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (date.today().isoformat(),))
        session_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, duration, weight) VALUES (?, ?, ?, ?, ?)",
            [
                (session_id, exercise_id, 1, 20, 16.0),
                (session_id, exercise_id, 2, 20, 20.0),
                (session_id, exercise_id, 3, 40, 12.0),
                (session_id, exercise_id, 4, 20.4, 18.0),
            ],
        )
        conn.commit()

        html = _page(auth_client)
        assert html.count('data-pb-exercise="ZZ timed PB"') == 1
        assert 'data-pb-effort="20" data-pb-weight="20.0"' in html
        assert 'data-pb-effort="40" data-pb-weight="12.0"' in html
        assert 'data-pb-effort="20" data-pb-weight="16.0"' not in html
        assert html.count('data-pb-effort="20"') == 1
        assert "20 s" in html
        assert "40 s" in html
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if exercise_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
        conn.commit()
        conn.close()


def test_no_picker_payload_is_embedded(auth_client):
    """The page used to embed every library row as JSON to feed the picker.

    Asserted through a positive control: the library is still populated (so a
    passing test means the payload is genuinely gone, not that the library
    happens to be empty).
    """
    conn = sqlite3.connect(user_db_path())
    try:
        lib_count = conn.execute("SELECT COUNT(*) FROM exercise_library WHERE archived = 0").fetchone()[0]
    finally:
        conn.close()
    assert lib_count > 0, "precondition: the library must be non-empty for this test to mean anything"

    html = _page(auth_client)
    assert "exercisePicker(" not in html, "the picker and its JSON payload were removed"
    assert "data-tag-filter" not in html, "tag filter chips lived in the picker"


def test_removed_write_endpoints_are_unroutable(auth_client):
    """The deleted endpoints must be gone from the router, not merely unlinked.

    A form removed from a template while its POST handler survives is still a
    live write path — reachable by anything holding the old URL.
    """
    token = csrf_token(auth_client, "/training")
    for path in (
        "/training/session",
        "/training/exercise",
        "/training/exercise/1/edit",
        "/training/exercise/1/delete",
    ):
        resp = auth_client.post(path, data={"_csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 404, f"{path} must be unroutable, got {resp.status_code}"


def test_session_delete_still_routes(auth_client):
    """Control for the test above: a path that IS still registered must not 404,
    so a blanket 404 (bad prefix, auth redirect) cannot make that test pass."""
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post("/training/session/999999999/delete", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code != 404, "the surviving delete route must still be registered"


def test_capture_copy_is_user_facing(auth_client):
    """`Zapisz i sparsuj` names the implementation. The user saves a workout.

    The one fact that makes a parse failure harmless - the note is committed
    before the LLM runs - was nowhere on the page.
    """
    html = auth_client.get("/training").text
    assert "Zapisz trening" in html
    assert "Zapisz i sparsuj" not in html
    assert "opcjonalnie" in html, "duration is optional and must say so"
    assert "Notatka zapisuje się" in html, "the user must know the note survives a failed parse"
