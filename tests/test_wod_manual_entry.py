"""A failed parse must leave a usable manual-entry path, not one blank line.

Reported after the truncation fix: when parsing fails the confirm screen offered
a single blank row whose only labelled action was "+ dodaj serię" - another SET
of one movement. A WOD is never one set of one movement, and reaching a second
*exercise* meant adding a "set" and then changing its select, so the only way in
described the wrong action.

The server was never the constraint: confirm_wod reads entry_0..entry_N-1 with a
per-row movement and accepts up to MAX_CONFIRM_ENTRIES. This was a template gap.
"""

import json
import re
import sqlite3
from urllib.parse import urlsplit

import pytest
from conftest import csrf_token, drain_jobs, user_db_path

from app.routers.training import MAX_CONFIRM_ENTRIES, SEED_ROWS_ON_PARSE_FAILURE


@pytest.fixture(autouse=True)
def _drop_probe_sessions():
    yield
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "DELETE FROM training_entries WHERE session_id IN "
            "(SELECT id FROM training_sessions WHERE notes LIKE 'ZZ %')"
        )
        conn.execute("DELETE FROM training_sessions WHERE notes LIKE 'ZZ %'")
        conn.commit()
    finally:
        conn.close()


def _fail_parse(monkeypatch):
    import app.services.wod_parser as wp

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        raise ValueError("LLM request timed out")

    monkeypatch.setattr(wp, "call_llm", fake_call_llm)


def _capture(auth_client, text="ZZ crossfit, parser down"):
    token = csrf_token(auth_client, "/training")
    captured = auth_client.post(
        "/training/wod",
        data={"date": "2026-08-03", "duration_minutes": "60", "wod_text": text, "_csrf_token": token},
    )
    # The parse is a durable job now, so the confirm page is only settled once
    # the worker has run.
    drain_jobs()
    return auth_client.get(captured.url)


def _session_id(html):
    match = re.search(r'name="session_id" value="(\d+)"', html)
    assert match, "confirm form must carry its session_id"
    return int(match.group(1))


def test_failed_parse_offers_several_blank_rows(auth_client, monkeypatch):
    """The regression: one row is not a manual-entry path."""
    _fail_parse(monkeypatch)
    resp = _capture(auth_client)
    assert resp.status_code == 200
    assert "Parsowanie nie powiodło się" in resp.text

    for i in range(SEED_ROWS_ON_PARSE_FAILURE):
        assert f'name="entry_{i}_movement"' in resp.text, f"seed row {i} must be rendered"
    assert f'name="entry_{SEED_ROWS_ON_PARSE_FAILURE}_movement"' not in resp.text, "and no more than that"
    assert SEED_ROWS_ON_PARSE_FAILURE > 1, "the whole point of this fix"

    # entry_count must agree with what was rendered, or confirm_wod's
    # `for i in range(entry_count)` silently drops the rows past it.
    assert f'name="entry_count" x-ref="entryCount"\n             value="{SEED_ROWS_ON_PARSE_FAILURE}"' in resp.text


def test_seed_rows_are_a_real_movement_picker(auth_client, monkeypatch):
    """Each blank row must offer the whole vocabulary, not just a skip option."""
    _fail_parse(monkeypatch)
    resp = _capture(auth_client)
    # Anchor inside the LAST seed row so this cannot pass on the first row alone.
    last = resp.text.split(f'name="entry_{SEED_ROWS_ON_PARSE_FAILURE - 1}_movement"')[1]
    # The option tag now also carries data-tags and data-recent for the search
    # box, so it no longer closes straight after the value. The property under
    # test is unchanged: every movement is pickable in every row.
    for name in ("Air Squat", "Pull-up", "Row", "Snatch"):
        assert f'<option value="{name}"' in last, f"{name} must be pickable in the last seed row"
    assert "<optgroup" in last, "the last seed row must group by section too"


def test_add_exercise_control_is_offered(auth_client, monkeypatch):
    """A labelled way to add a DIFFERENT movement, distinct from adding a set."""
    _fail_parse(monkeypatch)
    resp = _capture(auth_client)
    assert "+ dodaj ćwiczenie" in resp.text
    assert "addExercise()" in resp.text
    assert "+ dodaj serię" in resp.text, "and adding a set must still exist"
    # addExercise must start a fresh exercise, not clone-and-increment.
    assert "addRow('', 1)" in resp.text


def test_manual_rows_are_written_for_several_different_movements(auth_client, monkeypatch):
    """End to end: the rows a user types by hand after a failed parse persist,
    across multiple movements AND multiple sets of one of them."""
    _fail_parse(monkeypatch)
    session_id = _session_id(_capture(auth_client).text)

    token = csrf_token(auth_client, f"/training/wod/confirm/{session_id}")
    data = {
        "_csrf_token": token,
        "session_id": str(session_id),
        "entry_count": "4",
        # Two sets of one movement, then two other movements.
        "entry_0_movement": "Pull-up",
        "entry_0_set_number": "1",
        "entry_0_reps": "5",
        "entry_1_movement": "Pull-up",
        "entry_1_set_number": "2",
        "entry_1_reps": "5",
        "entry_2_movement": "Air Squat",
        "entry_2_set_number": "1",
        "entry_2_reps": "15",
        "entry_3_movement": "Row",
        "entry_3_set_number": "1",
        "entry_3_duration": "180",
    }
    resp = auth_client.post("/training/wod/confirm", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert "err=" not in resp.headers.get("location", ""), resp.headers.get("location")

    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT tex.name, te.set_number, te.reps, te.duration FROM training_entries te "
                "JOIN training_exercises tex ON te.exercise_id = tex.id "
                "WHERE te.session_id = ? ORDER BY te.id",
                (session_id,),
            )
        ]
    finally:
        conn.close()

    assert [(r["name"], r["set_number"]) for r in rows] == [
        ("Pull-up", 1),
        ("Pull-up", 2),
        ("Air Squat", 1),
        ("Row", 1),
    ]
    assert rows[0]["reps"] == 5
    assert rows[2]["reps"] == 15
    assert rows[3]["duration"] == 180


def test_unfilled_seed_rows_are_skipped_not_rejected(auth_client, monkeypatch):
    """The prose promises blank rows cost nothing. Submitting the seeded form with
    only one row filled must write that row and ignore the rest."""
    _fail_parse(monkeypatch)
    session_id = _session_id(_capture(auth_client).text)

    token = csrf_token(auth_client, f"/training/wod/confirm/{session_id}")
    data = {
        "_csrf_token": token,
        "session_id": str(session_id),
        "entry_count": str(SEED_ROWS_ON_PARSE_FAILURE),
        "entry_0_movement": "Burpee",
        "entry_0_set_number": "1",
        "entry_0_reps": "20",
    }
    # Every other seed row submitted exactly as rendered: blank movement, set 1.
    for i in range(1, SEED_ROWS_ON_PARSE_FAILURE):
        data[f"entry_{i}_movement"] = ""
        data[f"entry_{i}_set_number"] = "1"
        data[f"entry_{i}_reps"] = ""
        data[f"entry_{i}_weight"] = ""
        data[f"entry_{i}_duration"] = ""
        data[f"entry_{i}_note"] = ""

    resp = auth_client.post("/training/wod/confirm", data=data, follow_redirects=False)
    assert resp.status_code == 303
    assert "err=" not in resp.headers.get("location", "")

    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT tex.name, te.reps FROM training_entries te "
                "JOIN training_exercises tex ON te.exercise_id = tex.id WHERE te.session_id = ?",
                (session_id,),
            )
        ]
    finally:
        conn.close()
    assert [(r["name"], r["reps"]) for r in rows] == [("Burpee", 20)], "only the filled row is written"


def test_row_limit_is_shared_between_template_and_handler(auth_client, monkeypatch):
    """The client stops adding rows at the same number the handler accepts.

    Exceeding it rejects the WHOLE submission, so a template that let the user
    build 201 rows would cost them all 201. Pinning the shared source here means
    changing the handler's bound without the template cannot pass unnoticed.
    """
    _fail_parse(monkeypatch)
    resp = _capture(auth_client)
    assert f"wodConfirmForm({SEED_ROWS_ON_PARSE_FAILURE}, {MAX_CONFIRM_ENTRIES})" in resp.text

    session_id = _session_id(resp.text)
    token = csrf_token(auth_client, f"/training/wod/confirm/{session_id}")
    over = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": str(MAX_CONFIRM_ENTRIES + 1),
        },
        follow_redirects=False,
    )
    assert over.status_code == 303
    assert "err=" in over.headers["location"], "one past the bound must be refused, which is why the client caps"


def test_parse_failure_prose_describes_both_controls(auth_client, monkeypatch):
    """The screen has to say what the two buttons do - they are not
    self-evidently different, and the old copy described only adding sets."""
    _fail_parse(monkeypatch)
    resp = _capture(auth_client)
    assert "dodaj ćwiczenie" in resp.text
    assert "następną serię tego samego ruchu" in resp.text
    assert "Puste wiersze są pomijane przy zapisie" in resp.text
    assert "pusty wiersz do ręcznego wpisania" not in resp.text, "the singular copy is what this replaces"


def test_library_error_still_suppresses_the_seed_rows(auth_client, monkeypatch):
    """Unchanged guarantee: with no vocabulary there is nothing to pick, so rows
    that could not write anything must not be offered. Seeding several rows must
    not have turned one useless row into five."""
    _fail_parse(monkeypatch)
    session_id = _session_id(_capture(auth_client).text)

    import app.routers.training as tr

    async def no_movements(db):
        raise AssertionError("movement vocabulary has grown to 501 rows (max 500)")

    monkeypatch.setattr(tr, "canonical_movements", no_movements)
    resp = auth_client.get(f"/training/wod/confirm/{session_id}")
    assert resp.status_code == 200
    assert 'name="entry_0_movement"' not in resp.text
    assert "+ dodaj ćwiczenie" not in resp.text
    assert "lista ruchów" in resp.text


def test_parsed_rows_also_get_the_add_exercise_control(auth_client, monkeypatch):
    """A partial parse needs it most: the movements the parser missed have to be
    addable without hijacking a row that did parse."""
    import app.services.wod_parser as wp

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return json.dumps(
            {
                "entries": [
                    {"movement": "Row", "set_number": 1, "reps": None, "weight": None, "duration": 180.0, "note": ""}
                ],
                "unmatched": [],
            }
        )

    monkeypatch.setattr(wp, "call_llm", fake_call_llm)
    resp = _capture(auth_client)
    assert 'value="Row" selected' in resp.text
    assert "+ dodaj ćwiczenie" in resp.text
    assert f"wodConfirmForm(1, {MAX_CONFIRM_ENTRIES})" in resp.text


def test_stranded_session_offers_manual_entry(auth_client):
    """A session with a note, no entries and no pending parse must be recoverable.

    capture_wod commits the session before the LLM runs and stores the parse in a
    second commit. A crash between the two - a container recreated mid-parse,
    which this deployment does unattended - left the note with no form, no
    "dokończ" link and no route back.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        session_id = conn.execute(
            "INSERT INTO training_sessions (date, notes, wod_parsed) VALUES ('2026-08-24', 'ZZ stranded note', NULL)"
        ).lastrowid
        conn.commit()
    finally:
        conn.close()

    page = auth_client.get("/training").text
    assert f"/training/session/{session_id}/manual" in page, "no manual-entry route offered"

    resp = auth_client.post(
        f"/training/session/{session_id}/manual",
        data={"_csrf_token": csrf_token(auth_client, "/training")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert urlsplit(resp.headers["location"]).path == f"/training/wod/confirm/{session_id}"

    form = auth_client.get(f"/training/wod/confirm/{session_id}")
    assert form.status_code == 200
    assert 'name="entry_0_movement"' in form.text, "the armed session must render manual rows"


def test_manual_entry_refuses_a_session_that_already_has_entries(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        session_id = conn.execute(
            "INSERT INTO training_sessions (date, notes) VALUES ('2026-08-23', 'ZZ has entries')"
        ).lastrowid
        exercise_id = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, display_order) "
            "VALUES ('ZZ Manual Probe', 'Core', 'reps', 302)"
        ).lastrowid
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps) VALUES (?, ?, 1, 5)",
            (session_id, exercise_id),
        )
        conn.commit()
    finally:
        conn.close()

    resp = auth_client.post(
        f"/training/session/{session_id}/manual",
        data={"_csrf_token": csrf_token(auth_client, "/training")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert urlsplit(resp.headers["location"]).path == "/training"

    conn = sqlite3.connect(user_db_path())
    try:
        pending = conn.execute("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()
    assert pending is None, "a session with entries must not be re-armed"
