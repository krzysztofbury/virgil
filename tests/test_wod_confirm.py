"""POST /training/wod/confirm — writes entries and feeds volume/PBs."""

import sqlite3

from conftest import csrf_token, stat_value_for_label, user_db_path


def _query(sql, params=()):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _new_session(date_str="2026-07-30", notes="raw wod"):
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 60, ?)", (date_str, notes)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_writes_entries_and_creates_ad_hoc_exercise(auth_client):
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "21",
            "entry_0_weight": "43",
            "entry_0_duration": "",
            "entry_0_note": "21-15-9",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    entries = _query("SELECT * FROM training_entries WHERE session_id = ?", (session_id,))
    assert len(entries) == 1
    assert entries[0]["reps"] == 21
    assert entries[0]["weight"] == 43.0
    ex = _query("SELECT * FROM training_exercises WHERE id = ?", (entries[0]["exercise_id"],))
    assert ex[0]["name"] == "Thruster"
    assert ex[0]["ad_hoc"] == 1


def test_unknown_movement_is_skipped_without_creating_an_exercise(auth_client):
    session_id = _new_session()
    before = _query("SELECT COUNT(*) as c FROM training_exercises")[0]["c"]
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Devil Press",
            "entry_0_set_number": "1",
            "entry_0_reps": "10",
            "entry_0_weight": "22.5",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0
    assert _query("SELECT COUNT(*) as c FROM training_exercises")[0]["c"] == before


def test_ad_hoc_movement_is_absent_from_the_protocol_form(auth_client):
    # NOTE: this deliberately does NOT assert on the substring "Wall Ball" in the
    # rendered page. "Wall Ball" is seeded into exercise_library (migration 016,
    # category='CrossFit') and is therefore always rendered as an <option> in the
    # Core section's "Add exercise" picker dropdown on every page load, regardless
    # of whether this route ever ran. Asserting on that substring would pass even
    # if the ad_hoc exclusion filter in training_page() were deleted, and fail even
    # against a correct implementation — i.e. it's not load-bearing either way (see
    # tests/test_ad_hoc_visibility.py and the task-5 brief's test-hygiene notes).
    # Instead this exercises the confirm route end-to-end, then asserts on the
    # created row's own numeric id, which only appears in the protocol form's
    # per-exercise delete-form action when the exercise is an actual configured
    # protocol row (archived = 0 AND ad_hoc = 0).
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Wall Ball",
            "entry_0_set_number": "1",
            "entry_0_reps": "20",
            "entry_0_weight": "9",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    ex = _query("SELECT id, ad_hoc FROM training_exercises WHERE name = 'Wall Ball'")
    assert len(ex) == 1, "confirm route must create exactly one ad-hoc exercise row for a known movement"
    assert ex[0]["ad_hoc"] == 1

    page = auth_client.get("/training").text
    assert f"/training/exercise/{ex[0]['id']}/delete" not in page, (
        "ad-hoc movements must not enter the protocol form's configured-exercise rows"
    )


def test_entries_reach_the_weekly_volume_kpi(auth_client):
    """Confirmed WOD entries must reach the rendered /training page's Volume
    (Core) KPI and Personal Bests card — not just a hand-copied aggregate query
    run directly against sqlite (that exercises SQLite, not training.py, and
    would stay green even if training.py's own query regressed).
    """
    from datetime import date, timedelta

    monday = (date.today() - timedelta(days=date.today().weekday())).isoformat()
    session_id = _new_session(date_str=monday)
    baseline_volume = stat_value_for_label(auth_client.get("/training").text, "Volume (Core)")

    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Back Squat",
            "entry_0_set_number": "1",
            "entry_0_reps": "5",
            "entry_0_weight": "70",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )

    resp = auth_client.get("/training")
    new_volume = stat_value_for_label(resp.text, "Volume (Core)")
    assert new_volume == baseline_volume + 350, (
        f"5 reps x 70 kg must land in the rendered Core volume KPI; baseline={baseline_volume}, new={new_volume}"
    )

    pb_weight = stat_value_for_label(resp.text, "Back Squat")
    assert pb_weight == 70.0, "ad-hoc movements must reach the rendered Personal Bests card, not just volume"
