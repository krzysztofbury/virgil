"""POST /training/wod/confirm — writes entries and feeds volume/PBs."""

import json
import sqlite3
from datetime import date
from urllib.parse import unquote

from conftest import csrf_token, plain_stat_value_for_label, stat_value_for_label, user_db_path

from app.routers.training import MAX_CONFIRM_ENTRIES


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
    # tagged 'crossfit') and is therefore always rendered as an <option> in the
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


# --- "add a set" (usability defect): the client must be able to submit MORE
# rows than the confirm GET originally rendered. ---


def test_added_rows_beyond_server_rendered_count_are_written(auth_client):
    """Server contract behind the 'add a set' control: entry_count can
    legitimately exceed the number of rows the confirm GET originally
    rendered, as long as it reflects the true submitted row count and every
    entry_N_* field for i in range(entry_count) is present. This is exactly
    what Alpine's client-side "+ dodaj serię" button appends (see
    wod_confirm.html) — no server route change was needed for it, but nothing
    previously exercised a submission wider than what the GET rendered, so
    this is the regression guard for the reported defect: "only ever N rows,
    where N is server-rendered, could ever be submitted"."""
    session_id = _new_session()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "3",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "21",
            "entry_0_weight": "43",
            "entry_0_duration": "",
            "entry_0_note": "21-15-9",
            # Appended client-side off row 0: same movement, set_number + 1 —
            # exactly what clicking "+ dodaj serię" on row 0 produces.
            "entry_1_movement": "Thruster",
            "entry_1_set_number": "2",
            "entry_1_reps": "15",
            "entry_1_weight": "43",
            "entry_1_duration": "",
            "entry_1_note": "",
            # Appended but left on "— pomiń" (blank movement) — must be
            # skipped, exactly like an unmatched row nobody picked one for.
            "entry_2_movement": "",
            "entry_2_set_number": "3",
            "entry_2_reps": "",
            "entry_2_weight": "",
            "entry_2_duration": "",
            "entry_2_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    entries = _query(
        "SELECT set_number, reps, weight FROM training_entries WHERE session_id = ? ORDER BY set_number",
        (session_id,),
    )
    assert len(entries) == 2, f"expected the two filled-in rows written and the blank one skipped, got {entries}"
    assert [e["set_number"] for e in entries] == [1, 2]
    assert entries[0]["reps"] == 21 and entries[1]["reps"] == 15
    assert all(e["weight"] == 43.0 for e in entries)


def test_confirm_page_renders_the_add_set_control_and_alpine_wiring(auth_client):
    """GET /training/wod/confirm/{id} must ship the client-side "add a set"
    control: an Alpine component that can append a new row at the end of the
    table, and a hidden entry_count bound to the LIVE row count (base +
    however many extra rows got added) rather than the server-rendered
    constant baked in as a plain value.

    entry_count is submitted via belt-and-braces: an Alpine `:value` binding
    (the reactive path) AND an explicit `@submit` handler that re-asserts the
    same value on the ref'd input at the moment of submit (the fallback, in
    case the binding itself doesn't do what's expected — its failure mode is
    silent: a stale count makes confirm_wod's `for i in range(entry_count)`
    quietly drop the rows the user just added). Both halves must be present,
    or a future edit could silently delete the fallback as "redundant".

    A server-side test cannot click the button — this only proves the wiring
    is present in the rendered HTML, not that clicking it, or the @submit
    handler, actually fires correctly in a browser; that half is unverified
    here. The belt-and-braces (not a browser run) is what makes that
    acceptable: even if the reactive :value binding silently misbehaves in
    some browser, the explicit @submit assignment is a second, independent
    path to the same correct value.
    """
    session_id = _new_session(
        wod_parsed=json.dumps(
            {
                "entries": [
                    {"movement": "Thruster", "set_number": 1, "reps": 21, "weight": 43, "duration": None, "note": ""}
                ],
                "unmatched": [],
                "parse_error": "",
            }
        )
    )
    resp = auth_client.get(f"/training/wod/confirm/{session_id}")
    assert resp.status_code == 200
    html = resp.text
    assert f'x-data="wodConfirmForm(1, {MAX_CONFIRM_ENTRIES})"' in html, (
        "the Alpine component must be wired with the server-rendered count, and with the row bound "
        "the handler enforces - past it confirm_wod rejects the whole submission, losing every typed row"
    )
    assert "addSet($el)" in html, "each row must carry the add-a-set click handler"
    assert 'x-for="row in extraRows"' in html, "appended rows must be driven by an Alpine x-for template"
    assert ':value="base + extraRows.length"' in html, (
        "entry_count must be bound to the live row count, not a server-rendered constant"
    )
    assert 'x-ref="entryCount"' in html, (
        "the hidden entry_count input must be ref'd so the @submit fallback can reach it"
    )
    assert '@submit="$refs.entryCount.value = base + extraRows.length"' in html, (
        "the :value binding must be backed by an explicit @submit assignment — its failure mode is silent "
        "data loss (confirm_wod would drop the rows the user just added), so this belt-and-braces must stay"
    )


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


# --- Rejection must be distinguishable from consume-then-re-arm ---
#
# A mutation sweep showed the four test_out_of_range_* tests and the entry_count
# test all pass with their rejection branches deleted: rejection and
# "consumed, resolved nothing, re-armed" both produce 303 + ?err= + 0 entries +
# wod_parsed still set. The re-arm added on this branch made them vacuous
# without touching them. These pin the difference.


def _capture_two_row_parse(auth_client, monkeypatch, note):
    from test_wod_capture import _sessions, _stub_llm

    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": 21, "weight": 43.0, "duration": None, "note": ""},
                {"movement": "Pull-up", "set_number": 1, "reps": 21, "weight": None, "duration": None, "note": ""},
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    auth_client.post("/training/wod", data={"date": date.today().isoformat(), "wod_text": note, "_csrf_token": token})
    return _sessions()[0]["id"], token


def test_a_rejected_field_never_reaches_the_consume(auth_client, monkeypatch, caplog):
    """The docstring's claim — validation runs BEFORE the B1 consume — was untested.

    Without this, deleting the `return` from the _ConfirmRejected handler still
    passes: execution falls through, the parse is consumed, and the re-arm
    produces the same 303 + err the rejection would have.
    """
    session_id, token = _capture_two_row_parse(auth_client, monkeypatch, "ZZ reject-before-consume")

    with caplog.at_level("WARNING", logger="app.routers.training"):
        caplog.clear()
        resp = auth_client.post(
            "/training/wod/confirm",
            data={
                "_csrf_token": token,
                "session_id": str(session_id),
                "entry_count": "2",
                "entry_0_movement": "Thruster",
                "entry_0_set_number": "1",
                "entry_0_reps": "21",
                "entry_0_weight": "43",
                "entry_0_duration": "",
                "entry_0_note": "",
                "entry_1_movement": "Pull-up",
                "entry_1_set_number": "1",
                "entry_1_reps": "99999",
                "entry_1_weight": "",
                "entry_1_duration": "",
                "entry_1_note": "",
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "poza zakresem" in unquote(resp.headers["location"]), "the rejection names the offending field"
    assert "parse re-armed" not in caplog.text, (
        "a rejected submission must never reach the consume — re-arming means it did"
    )
    assert _query("SELECT COUNT(*) AS n FROM training_entries WHERE session_id = ?", (session_id,))[0]["n"] == 0, (
        "the valid row must not be written when a sibling row is rejected"
    )


def test_out_of_range_entry_count_never_reaches_the_consume(auth_client, monkeypatch, caplog):
    """Same masking: with the reject branch replaced by `entry_count = 0`, the
    loop writes nothing, the consume succeeds, and the re-arm emits the same
    303 + err. That is the exact B2 defect this route was built to prevent."""
    session_id, token = _capture_two_row_parse(auth_client, monkeypatch, "ZZ entry-count-before-consume")

    with caplog.at_level("WARNING", logger="app.routers.training"):
        caplog.clear()
        resp = auth_client.post(
            "/training/wod/confirm",
            data={"_csrf_token": token, "session_id": str(session_id), "entry_count": "201"},
            follow_redirects=False,
        )
    assert "Zbyt dużo wpisów" in unquote(resp.headers["location"])
    assert "parse re-armed" not in caplog.text, "an over-bound entry_count must be refused before the consume"


def test_discard_survives_an_out_of_range_entry_count(auth_client, monkeypatch):
    """Discard is hoisted above entry_count validation, not just above the field
    loop. The existing test only pushes an out-of-range entry *field*, so moving
    discard back below the entry_count check went unnoticed."""
    session_id, token = _capture_two_row_parse(auth_client, monkeypatch, "ZZ discard-over-entry-count")
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, "session_id": str(session_id), "entry_count": "201", "action": "discard"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/training", "discard must outrank every field check, entry_count included"
    assert _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"] is None


def test_volume_kpi_counts_only_core_reps_movements(auth_client):
    """K01/K02/K03: the KPI tests all use a before/after delta and log a Core
    reps movement, so dropping `section = 'Core'` or `metric = 'reps'` shifts
    baseline and new by the same amount and the delta still matches. A Cardio
    entry must move Total Reps and leave Volume (Core) alone.
    """
    conn = sqlite3.connect(user_db_path())
    ex_id = session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES (?, 'Cardio', 'reps', 1)",
            ("ZZTestCardioForKpi",),
        )
        ex_id = cur.lastrowid
        cur = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (date.today().isoformat(),))
        session_id = cur.lastrowid
        conn.commit()

        page = auth_client.get("/training").text
        vol_before = stat_value_for_label(page, "Volume (Core)")
        reps_before = plain_stat_value_for_label(page, "Total Reps")

        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, 1, 30, 20)",
            (session_id, ex_id),
        )
        conn.commit()

        page = auth_client.get("/training").text
        assert plain_stat_value_for_label(page, "Total Reps") == reps_before + 30, (
            "a Cardio reps entry counts toward reps"
        )
        assert stat_value_for_label(page, "Volume (Core)") == vol_before, (
            "Volume (Core) must ignore non-Core sections — a delta-only test cannot see this"
        )

        # A metric='time' movement carrying a reps value must be excluded from
        # Total Reps. Without this the metric filter can be dropped entirely and
        # nothing notices: every other fixture uses metric='reps', which counts
        # either way.
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES (?, 'Core', 'time', 1)",
            ("ZZTestTimeMetricForKpi",),
        )
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, 1, 45, 16)",
            (session_id, cur.lastrowid),
        )
        conn.commit()

        page = auth_client.get("/training").text
        assert plain_stat_value_for_label(page, "Total Reps") == reps_before + 30, (
            "a metric='time' movement's reps must not reach Total Reps"
        )
        assert stat_value_for_label(page, "Volume (Core)") == vol_before, (
            "nor its weight reach Volume (Core), even though its section is Core"
        )
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        conn.execute("DELETE FROM training_exercises WHERE name LIKE 'ZZTest%ForKpi'")
        if ex_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (ex_id,))
        conn.commit()
        conn.close()


def test_personal_best_is_the_maximum_not_the_first_or_lowest(auth_client):
    """P03/P05: every PB fixture logs exactly one set, so MIN and MAX agree and
    GROUP BY is a no-op. Two sets at different weights separate them."""
    conn = sqlite3.connect(user_db_path())
    ex_id = session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES (?, 'Core', 'reps', 1)",
            ("ZZTestPbMaximum",),
        )
        ex_id = cur.lastrowid
        cur = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (date.today().isoformat(),))
        session_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, 5, ?)",
            [(session_id, ex_id, 1, 60.0), (session_id, ex_id, 2, 85.0), (session_id, ex_id, 3, 70.0)],
        )
        conn.commit()

        assert stat_value_for_label(auth_client.get("/training").text, "ZZTestPbMaximum") == 85.0, (
            "the PB must be the heaviest set, not the first or the lightest"
        )
    finally:
        if session_id is not None:
            conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        if ex_id is not None:
            conn.execute("DELETE FROM training_exercises WHERE id = ?", (ex_id,))
        conn.commit()
        conn.close()


def test_delete_session_actually_deletes(auth_client):
    """E21: the only delete test asserts the route is registered (!= 404).
    Replacing the DELETE with `pass` left the suite green."""
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, notes) VALUES (?, 'ZZ delete-me')", (date.today().isoformat(),)
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    token = csrf_token(auth_client, "/training")
    auth_client.post(f"/training/session/{session_id}/delete", data={"_csrf_token": token}, follow_redirects=False)
    assert _query("SELECT id FROM training_sessions WHERE id = ?", (session_id,)) == [], (
        "the delete route must remove the row, not merely exist"
    )


def test_rendered_confirm_form_carries_every_field_the_route_needs(auth_client, monkeypatch):
    """W25/G01: no test in the suite ever submits a rendered form — every POST
    body is hand-built — so the template's field names and form action are
    uncoupled from the route that reads them. Removing the session_id hidden
    input left the suite green while every real submission silently wrote
    nothing.
    """
    import re as _re

    session_id, _token = _capture_two_row_parse(auth_client, monkeypatch, "ZZ rendered form contract")
    html = auth_client.get(f"/training/wod/confirm/{session_id}").text
    form = html[html.index('action="/training/wod/confirm"') :]
    form = form[: form.index("</form>")]

    hidden = set(_re.findall(r'name="(_csrf_token|session_id|entry_count)"', form))
    assert hidden == {"_csrf_token", "session_id", "entry_count"}, (
        f"the rendered form must carry every field confirm_wod reads, got {sorted(hidden)}"
    )
    assert _re.search(r'name="session_id"[^>]*\svalue="\d+"', form), "session_id must carry a server-rendered value"


def test_err_toast_is_json_escaped(auth_client):
    """A backslash in ?err= must reach showToast intact, not truncate the message.

    The value used to be interpolated straight into a JavaScript string literal.
    Autoescape stops XSS there, but it does not stop a backslash from starting an
    escape sequence and eating the rest of the message.
    """
    session_id = _new_session()
    resp = auth_client.get(f"/training/wod/confirm/{session_id}?err=path%20C%3A%5Ctmp%20failed")
    assert resp.status_code == 200
    assert r"C:\\tmp failed" in resp.text
    assert r'showToast("path C:\tmp' not in resp.text
