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


def _new_session(
    date_str="2026-07-30", notes="raw wod", wod_parsed='{"entries": [], "unmatched": [], "parse_error": ""}'
):
    """A session as POST /training/wod leaves it: wod_parsed set (non-NULL)
    marks it as having a pending confirm. confirm_wod (B1) now requires this
    to be non-NULL before it will write anything — a bare INSERT without it
    would look like an already-confirmed/replayed session and get silently
    redirected without writing.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 60, ?, ?)",
            (date_str, notes, wod_parsed),
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


# --- B1: replay safety ---


def _confirm_thruster_payload(session_id, **overrides):
    data = {
        "session_id": str(session_id),
        "entry_count": "1",
        "entry_0_movement": "Thruster",
        "entry_0_set_number": "1",
        "entry_0_reps": "21",
        "entry_0_weight": "43",
        "entry_0_duration": "",
        "entry_0_note": "21-15-9",
    }
    data.update(overrides)
    return data


def test_confirm_consumes_wod_parsed_on_success(auth_client):
    """B1 happy path: a successful confirm must flip wod_parsed to NULL so a
    replay can be told apart from a first, legitimate write."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]
    assert row["wod_parsed"] is None, "a confirmed session's wod_parsed must be cleared"
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 1


def test_replaying_confirm_post_does_not_duplicate_entries(auth_client):
    """B1 reproduction: two identical POSTs to /training/wod/confirm (double
    submit, or Back-then-resubmit on the now-permanent confirm URL) must
    write ONE set of entries, not two. Before the fix, the write checked only
    that the session existed — nothing marked it confirmed — so 21 reps x
    43 kg landed twice: 1806 kg of volume instead of 903."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    payload = {"_csrf_token": token, **_confirm_thruster_payload(session_id)}

    first = auth_client.post("/training/wod/confirm", data=payload, follow_redirects=False)
    assert first.status_code == 303
    second = auth_client.post("/training/wod/confirm", data=payload, follow_redirects=False)
    assert second.status_code == 303, "a replay must still redirect cleanly, just without writing"

    entries = _query("SELECT * FROM training_entries WHERE session_id = ?", (session_id,))
    assert len(entries) == 1, f"replay must not duplicate entries; got {len(entries)}"


def test_confirm_on_unknown_session_is_a_noop(auth_client):
    """A session_id with no matching row (or one belonging to nothing, e.g. a
    tampered id) must be indistinguishable from a replay: redirect, no write."""
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(999999)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (999999,))[0]["c"] == 0


# --- B2: out-of-range values must be rejected loudly, not silently dropped ---


def test_entry_count_out_of_range_is_rejected_not_silently_zeroed(auth_client):
    """B2 reproduction: entry_count=201 (over the [0,200] bound) used to
    become `or 0` — a 303 with zero entries written and no log line, even
    though the user's reviewed rows were all well-formed. It must now be
    rejected loudly instead, and the session's wod_parsed must survive so the
    confirm screen is still there to retry against."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id, entry_count="201")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith(f"/training/wod/confirm/{session_id}")
    assert "err=" in location, "the rejection must be surfaced (?err=), not a silent redirect"
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0
    row = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]
    assert row["wod_parsed"] is not None, "a rejected submission must not consume wod_parsed — the user can retry"


def test_out_of_range_reps_rejects_the_whole_submission(auth_client):
    """B2 reproduction: reps=1500 (over REPS_MAX=1000) used to parse to None
    and get stored as `reps IS NULL` — silently contributing nothing to
    Volume or Total Reps despite looking like a normal saved entry. It must
    now reject the whole submission instead of storing a null-reps row."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id, entry_0_reps="1500")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0
    row = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]
    assert row["wod_parsed"] is not None


def test_out_of_range_set_number_rejects_the_whole_submission(auth_client):
    """B2 reproduction: set_number=150 used to clamp to `or 1`, silently
    colliding with the real set 1 instead of being reported."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id, entry_0_set_number="150")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0


def test_out_of_range_weight_rejects_the_whole_submission(auth_client):
    """B2 reproduction, float path: only the integer fields (reps, set_number)
    had coverage proving out-of-range values are rejected loudly rather than
    silently nulled — the float fields (weight, duration) shared the same
    `_confirm_float` helper but had no test. weight=1500 (over
    WEIGHT_KG_MAX=1000) used to parse to None and get stored as a null-weight
    row, exactly the B2 defect this branch already fixed for reps. It must
    reject the whole submission instead."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id, entry_0_weight="1500")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0
    row = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]
    assert row["wod_parsed"] is not None, "a rejected submission must not consume wod_parsed — the user can retry"


def test_out_of_range_duration_rejects_the_whole_submission(auth_client):
    """B2 reproduction, float path: duration=100000 (over
    DURATION_SECONDS_MAX=86400) must reject the whole submission instead of
    storing a null-duration row — the other half of the float coverage gap
    alongside weight above."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, **_confirm_thruster_payload(session_id, entry_0_duration="100000")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    assert _query("SELECT COUNT(*) as c FROM training_entries WHERE session_id = ?", (session_id,))[0]["c"] == 0
    row = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]
    assert row["wod_parsed"] is not None, "a rejected submission must not consume wod_parsed — the user can retry"
