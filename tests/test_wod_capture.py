"""POST /training/wod — the note is persisted before the LLM is ever called."""

import asyncio
import concurrent.futures
import json
import re
import sqlite3
from datetime import date
from urllib.parse import unquote

import pytest
from conftest import csrf_token, user_db_path


@pytest.fixture(autouse=True)
def _drop_probe_sessions():
    """Remove this file's own probe sessions after each test.

    auth_client is session-scoped over one shared DB, so sessions left with
    wod_parsed armed accumulate across the file. That residue is not inert: it
    fills /training with 'dokończ' links, which is what made an ungated link
    indistinguishable from a gated one in this file's own tests.
    """
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


def _sessions():
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM training_sessions ORDER BY id DESC")]
    finally:
        conn.close()


def _query(sql, params=()):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _stub_llm(monkeypatch, payload=None, exc=None, raw=None):
    import app.services.wod_parser as wp

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        if exc:
            raise exc
        if raw is not None:
            return raw
        return json.dumps(payload)

    monkeypatch.setattr(wp, "call_llm", fake_call_llm)


def test_saves_raw_text_and_shows_parsed_entries(auth_client, monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {
                    "movement": "Thruster",
                    "set_number": 1,
                    "reps": 21,
                    "weight": 43.0,
                    "duration": None,
                    "note": "21-15-9",
                }
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "21-15-9 thruster 43 kg, 8:42",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    # Mapping-sensitive: "Thruster" alone is a false-positive magnet — it is one of
    # the 31 canonical movements rendered as an <option> in every entry row's
    # <select> regardless of what was actually parsed. Assert the specific option
    # is the one marked selected, and that the parsed reps landed on entry 0.
    assert 'value="Thruster" selected' in resp.text
    assert 'name="entry_0_reps" value="21"' in resp.text
    latest = _sessions()[0]
    assert latest["notes"] == "21-15-9 thruster 43 kg, 8:42"
    assert latest["duration_minutes"] == 60


def test_note_survives_an_llm_failure(auth_client, monkeypatch):
    _stub_llm(monkeypatch, exc=ValueError("LLM request timed out"))
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "55",
            "wod_text": "5x5 back squat 70, potem metcon",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    latest = _sessions()[0]
    assert latest["notes"] == "5x5 back squat 70, potem metcon", "raw text must be persisted before parsing"
    assert "timed out" in resp.text or "parsowanie" in resp.text.lower()
    assert "uzupełnić wpisy ręcznie" not in resp.text, (
        "I3: the old parse_error message claimed a manual-entry path that did not "
        "exist. The path it points at now — the seeded row on this very confirm "
        "screen — is real; see test_parse_failure_still_offers_a_usable_entry_row."
    )
    assert "historii treningów" in resp.text, "the parse_error message must point to where the note actually is"


def test_garbled_llm_response_still_saves_the_note(auth_client, monkeypatch):
    """parse_andy_response raises ValueError when the reply can't be coerced into a
    JSON object at all (no LLM exception, just a garbled/refusal-style reply) — this
    is a distinct failure path from an LLM/provider error and must be equally harmless
    to the user's note."""
    _stub_llm(monkeypatch, raw="I'm sorry, I can't help with that")
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "50",
            "wod_text": "3 rundy: 10 burpees, 15 kb swing 24",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    latest = _sessions()[0]
    assert latest["notes"] == "3 rundy: 10 burpees, 15 kb swing 24", "raw text must survive a garbled LLM reply"
    assert "did not return a JSON object" in resp.text or "parsowanie" in resp.text.lower()


def test_note_survives_a_non_valueerror_crash(auth_client, monkeypatch):
    """capture_wod catches `Exception`, not just `ValueError`, around parse_wod
    (review finding I3). Before that fix, a non-ValueError crash — real, not
    hypothetical: app/services/llm.py has bare asserts (missing content,
    max_tokens bounds) and transport errors that aren't litellm.APIError
    subclasses — propagated as a 500. The INSERT+commit had already happened
    (note intact), but `wod_parsed` stayed NULL, so
    GET /training/wod/confirm/{id} redirected away forever, and the
    per-set log form that existed at the time always INSERTed a brand-new
    session — there was no way back to this one. (That form has since been
    removed; the confirm screen is now the only writer.)

    This test proves the crash no longer strands the session: the request now
    completes (the PRG redirect lands on a 200 confirm page, not a 500), the
    raw note survives, and the confirm screen — reachable, not a dead loop —
    is what actually renders, carrying the crash message as parse_error.

    (This test previously asserted `pytest.raises(RuntimeError)` to prove
    ordering by way of an escaping exception. Broadening the except clause
    means nothing escapes anymore, so that mechanism no longer applies — the
    ordering guarantee itself is unchanged (INSERT+commit still precedes the
    try/except), it's just no longer observable through an uncaught crash.)
    """
    import app.services.wod_parser as wp

    async def boom(db, system_prompt, user_prompt, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(wp, "call_llm", boom)
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "50",
            "wod_text": "5 rund: 10 burpee, 15 wall ball",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200, "the crash must not 500 — it must land on the confirm screen via the PRG redirect"
    assert "connection reset by peer" in resp.text or "parsowanie" in resp.text.lower()
    latest = _sessions()[0]
    assert latest["notes"] == "5 rund: 10 burpee, 15 wall ball"
    assert latest["wod_parsed"], "wod_parsed must be set even on a non-ValueError crash, or the confirm GET 303s away"


def test_insert_precedes_parse_a_baseexception_still_cannot_erase_the_note(auth_client, monkeypatch):
    """The branch's headline invariant: the training_sessions INSERT+commit must
    precede parse_wod, not just for the ValueError/Exception paths (I3's
    broadened `except Exception` absorbs those, so they no longer prove
    anything escapes) but for BaseException and process death, which
    `except Exception` cannot catch either.

    `pytest.raises(RuntimeError)` used to be the sole oracle for this ordering,
    by way of an escaping exception. I3 broadened the handler so nothing
    escapes anymore, and nothing replaced the oracle — this invariant has lost
    its coverage three times on this branch. asyncio.CancelledError inherits
    from BaseException (Python 3.8+), so stubbing call_llm to raise it
    reproduces exactly the case `except Exception` cannot absorb: the
    exception must escape capture_wod entirely, all the way through the test
    client, and the session row must already exist despite that.

    (TestClient runs the ASGI app on a background event loop and bridges it to
    this thread via a concurrent.futures.Future; a BaseException raised inside
    the coroutine marks that asyncio Task cancelled, so what actually surfaces
    here is concurrent.futures.CancelledError, not the original
    asyncio.CancelledError instance — that's a property of the thread bridge,
    not of capture_wod. Either way, SOME exception escaping is the proof that
    `except Exception` did not swallow it; a 200 response would mean it did.)

    If the INSERT+commit is ever moved to after the try/except (so parse_wod
    runs first), this crash pre-empts the INSERT and no session row is ever
    created — this test must then fail even though every other WOD capture
    test stays green.
    """
    import app.services.wod_parser as wp

    async def boom(db, system_prompt, user_prompt, **kwargs):
        raise asyncio.CancelledError("simulated task cancellation mid-parse")

    monkeypatch.setattr(wp, "call_llm", boom)
    token = csrf_token(auth_client, "/training")
    before = len(_sessions())
    wod_text = "cancelled-error ordering repro: 5 rund 10 burpee"

    with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
        auth_client.post(
            "/training/wod",
            data={
                "date": "2026-07-30",
                "duration_minutes": "50",
                "wod_text": wod_text,
                "_csrf_token": token,
            },
        )

    sessions = _sessions()
    assert len(sessions) == before + 1, (
        "the training_sessions row must be committed BEFORE parse_wod runs, "
        "so it survives even a BaseException that escapes capture_wod entirely"
    )
    assert sessions[0]["notes"] == wod_text
    assert sessions[0]["wod_parsed"] is None, (
        "wod_parsed is only written after the try/except returns; a crash that "
        "escapes must leave it NULL, not fabricate a parse result"
    )


def test_capture_form_renders_on_training_page(auth_client):
    """A wrong action= or renamed field on the WOD capture form would ship green
    without this — nothing else in the suite posts to /training/wod via the
    rendered form, only via a hand-built payload."""
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    assert 'action="/training/wod"' in resp.text
    assert 'name="wod_text"' in resp.text


def test_empty_text_creates_no_session(auth_client):
    before = len(_sessions())
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={"date": "2026-07-30", "duration_minutes": "60", "wod_text": "   ", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(_sessions()) == before


def test_no_llm_provider_still_saves_the_note(auth_client):
    """conftest pins VIRGIL_INTERNAL_LLM_KEY='' — with call_llm unstubbed the
    provider lookup raises, which is exactly the 'LLM unavailable' path."""
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "45",
            "wod_text": "row 2k, potem 3 rundy",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert _sessions()[0]["notes"] == "row 2k, potem 3 rundy"


def test_unmatched_movements_are_surfaced(auth_client, monkeypatch):
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "devil press 10",
            "_csrf_token": token,
        },
    )
    assert "Devil Press" in resp.text


def test_entries_empty_unmatched_present_renders_editable_row_not_dead_end(auth_client, monkeypatch):
    """entries == [] with unmatched != [] is the 'nothing recognised' case. The
    confirm form must still render — with a select (defaulting to a skip
    option) and blank reps/weight/duration inputs for the unmatched movement —
    instead of falling through to the dead-end 'nic nie udało się' message
    that the old {% if entries %} guard produced.
    """
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "devil press 10",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert "Devil Press" in resp.text
    assert 'name="entry_0_movement"' in resp.text, "the unmatched movement must render as an editable row"
    assert 'option value=""' in resp.text, "the row's select must offer an empty (skip) option"
    assert "Zapisz wpisy" in resp.text, "the confirm form must render, not the dead-end fallback"
    assert "Nic nie udało się sparsować" not in resp.text


def test_all_rows_skipped_on_confirm_creates_no_entry(auth_client, monkeypatch):
    """Submitting with every row left on '— pomiń' must create no entries.

    Renamed from test_unmatched_row_skipped_on_confirm_creates_no_entry. It still
    reaches the resolve_movement -> continue path for every row, but the outcome
    changed: the parse is consumed, nothing resolves, and the parse is re-armed
    with a redirect back to the form. Both assertions here (303, no entries) hold
    across all three behaviours this test has seen, which is why it never noticed
    any of the changes.

    The per-row skip path it was named for is covered by
    test_skipping_a_parsed_entry_drops_only_that_row, which mixes a named row
    with a skipped one.
    """
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "devil press 10",
            "_csrf_token": token,
        },
    )
    session_id = _sessions()[0]["id"]

    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "",
            "entry_0_set_number": "1",
            "entry_0_reps": "",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    import sqlite3

    from conftest import user_db_path

    conn = sqlite3.connect(user_db_path())
    try:
        count = conn.execute("SELECT COUNT(*) FROM training_entries WHERE session_id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "skipping the unmatched row must create no training_entries row"


def test_parsed_entry_select_offers_a_skip_option(auth_client, monkeypatch):
    """I4 reproduction: the parsed-entry <select> (unlike the unmatched-row
    select, which already had one) had no empty option. If the model emitted
    a bogus entry — e.g. three 'Burpee' rows from a warm-up the user didn't
    mean to log — the only ways forward were to retype it as some other real
    movement or accept it; abandoning the screen would have discarded the
    correct entries too.
    """
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": 21, "weight": 43.0, "duration": None, "note": ""},
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "thruster 21 43kg",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200
    assert 'name="entry_0_movement"' in resp.text
    # Scoped to entry_0's own <select> — the unmatched-row select already had
    # an empty option before this fix, so a page-wide substring check would
    # have passed even without it on the parsed-entry select.
    select_start = resp.text.index('name="entry_0_movement"')
    select_html = resp.text[select_start : resp.text.index("</select>", select_start)]
    assert '<option value="">' in select_html, "the parsed-entry select must offer an empty (skip) option"


def test_skipping_a_parsed_entry_drops_only_that_row(auth_client, monkeypatch):
    """I4: setting a parsed entry's movement to the skip option must write no
    row for it while a sibling entry in the same submission still writes."""
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {
                    "movement": "Burpee",
                    "set_number": 1,
                    "reps": 10,
                    "weight": None,
                    "duration": None,
                    "note": "warm-up",
                },
                {"movement": "Thruster", "set_number": 1, "reps": 21, "weight": 43.0, "duration": None, "note": ""},
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "burpee warm-up, thruster 21 43kg",
            "_csrf_token": token,
        },
    )
    session_id = _sessions()[0]["id"]

    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "2",
            "entry_0_movement": "",  # user picked "— pomiń" for the unwanted Burpee row
            "entry_0_set_number": "1",
            "entry_0_reps": "",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
            "entry_1_movement": "Thruster",
            "entry_1_set_number": "1",
            "entry_1_reps": "21",
            "entry_1_weight": "43",
            "entry_1_duration": "",
            "entry_1_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        entries = [
            dict(r)
            for r in conn.execute(
                "SELECT te.*, tex.name AS exercise_name FROM training_entries te "
                "JOIN training_exercises tex ON te.exercise_id = tex.id WHERE te.session_id = ?",
                (session_id,),
            )
        ]
    finally:
        conn.close()
    assert len(entries) == 1, "only the non-skipped sibling entry must be written"
    assert entries[0]["exercise_name"] == "Thruster"


def test_wod_redirects_to_confirm_and_get_does_not_reparse(auth_client, monkeypatch):
    """POST /training/wod is Post/Redirect/Get: it must 303 to a confirm URL
    instead of rendering HTML directly — a raw 200 means replaying the POST
    (double submit, or a browser's resubmission prompt on refresh) fires the
    parser, and the paid LLM call, a second time. The GET the redirect points
    to must render the STORED parse result and never invoke the parser again:
    a plain page refresh must never cost money.
    """
    calls = {"n": 0}

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        calls["n"] += 1
        return json.dumps(
            {
                "entries": [
                    {
                        "movement": "Thruster",
                        "set_number": 1,
                        "reps": 21,
                        "weight": 43.0,
                        "duration": None,
                        "note": "",
                    }
                ],
                "unmatched": [],
            }
        )

    import app.services.wod_parser as wp

    monkeypatch.setattr(wp, "call_llm", fake_call_llm)

    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "60",
            "wod_text": "21-15-9 thruster 43kg",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, "POST /training/wod must Post/Redirect/Get, not render HTML directly"
    location = resp.headers["location"]
    assert location.startswith("/training/wod/confirm/")
    assert calls["n"] == 1

    first = auth_client.get(location)
    assert first.status_code == 200
    assert 'value="Thruster" selected' in first.text
    assert calls["n"] == 1, "the confirm GET must not re-invoke the parser"

    # A refresh (second GET on the same URL) must still not re-parse.
    second = auth_client.get(location)
    assert second.status_code == 200
    assert 'value="Thruster" selected' in second.text
    assert calls["n"] == 1, "a refresh of the confirm screen must never cost another LLM call"


def test_confirm_get_unknown_session_redirects_to_training(auth_client):
    resp = auth_client.get("/training/wod/confirm/999999", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/training"


def test_confirm_get_with_library_over_bound_renders_instead_of_500ing(auth_client):
    """Merge-blocker: canonical_movements() (app/services/wod_parser.py) asserts
    the CrossFit vocabulary stays within MAX_LIBRARY_MOVEMENTS (I5) — but
    POST /api/library is MCP-callable, so the library can grow past that bound
    AFTER a session is captured and BEFORE the user opens the confirm screen.
    capture_wod's broadened `except Exception` (I3) absorbs the AssertionError
    during parsing, so the note and wod_parsed are saved fine; but
    wod_confirm_page called canonical_movements() again, unguarded, to build
    the picker — reopening a permanent 500 on the very GET that I3 was written
    to keep reachable. This must now degrade instead: 200, not 500, with the
    note still intact and a message telling the user it's safe.
    """
    conn = sqlite3.connect(user_db_path())
    session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 60, ?, ?)",
            ("2026-07-30", "over-bound repro note", '{"entries": [], "unmatched": [], "parse_error": ""}'),
        )
        conn.commit()
        session_id = cur.lastrowid

        max_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) FROM exercise_library").fetchone()[0]
        conn.executemany(
            "INSERT INTO exercise_library (section, name, display_order, metric, builtin) "
            "VALUES ('Core', ?, ?, 'reps', 0)",
            [(f"Over Bound Movement {i}", max_order + i + 1) for i in range(500)],
        )
        conn.commit()

        resp = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
        assert resp.status_code == 200, "an oversized library must not 500 the confirm GET — the note is already safe"
        assert "zapisana" in resp.text, "the degraded confirm screen must reassure the user the note is safe"
    finally:
        conn.execute("DELETE FROM exercise_library WHERE name LIKE 'Over Bound Movement %'")
        if session_id is not None:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()


def test_confirm_get_corrupt_wod_parsed_redirects_instead_of_500ing(auth_client):
    """M3 reproduction: json.loads(session['wod_parsed']) was unguarded — a
    corrupt stored value would 500 GET /training/wod/confirm/{id} forever
    (there is no way to fix the stored column from the UI). It must instead
    redirect the same way the 'no stored result at all' case already does.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 60, ?, ?)",
            ("2026-07-30", "corrupt cache repro", "{not valid json"),
        )
        conn.commit()
        session_id = cur.lastrowid
    finally:
        conn.close()

    resp = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/training"


def test_parse_failure_still_offers_a_usable_entry_row(auth_client, monkeypatch):
    """A failed parse must leave a way to record sets, not a dead end.

    POST /training/wod/confirm is the only route in the codebase that writes
    training_entries. The confirm form used to be gated on
    `entries|length + unmatched|length > 0`, so an LLM outage rendered no form at
    all — and "+ dodaj serię" clones an existing <tr>, so there was nothing to
    clone either. Weekly volume and Personal Bests would simply stop moving,
    silently, for as long as the provider was down.

    Worse, the on-screen message told the user to delete the session and log the
    workout manually; the manual path had been removed, so following it destroyed
    the saved note and returned nothing.
    """
    _stub_llm(monkeypatch, exc=ValueError("provider unavailable"))
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-30",
            "duration_minutes": "50",
            "wod_text": "cos czego parser nie ruszy",
            "_csrf_token": token,
        },
    )
    assert resp.status_code == 200

    assert 'action="/training/wod/confirm"' in resp.text, (
        "the confirm form must render even when nothing was parsed — it is the only route that writes training_entries"
    )
    assert 'name="entry_0_movement"' in resp.text, "a blank editable row must be seeded"
    assert "wodConfirmForm(1)" in resp.text, (
        "the Alpine row counter must account for the seeded row — at 0, '+ dodaj serię' "
        "mints entry_0_* a second time and the two rows collide on submit"
    )
    assert 'name="entry_0_reps"' in resp.text
    assert 'name="entry_count"' in resp.text

    assert "usuń tę sesję" not in resp.text, "the message must not instruct the user to destroy the saved note"


def test_seeded_row_can_actually_save_an_entry(auth_client, monkeypatch):
    """The seed row is only worth having if submitting it writes an entry.

    Asserting the HTML alone would pass against a row whose field names the
    confirm route ignores.
    """
    _stub_llm(monkeypatch, exc=ValueError("provider unavailable"))
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={
            "date": "2026-07-29",
            "wod_text": "ZZ manual fallback session",
            "_csrf_token": token,
        },
    )
    session_id = _sessions()[0]["id"]

    before = _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"]
    movement = _query("SELECT name FROM exercise_library WHERE archived = 0 ORDER BY name LIMIT 1")[0]["name"]

    auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": movement,
            "entry_0_set_number": "1",
            "entry_0_reps": "12",
            "entry_0_weight": "40",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    after = _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"]
    assert after == before + 1, "submitting the seeded row must create exactly one entry"


def test_blank_submit_does_not_strand_the_session(auth_client, monkeypatch):
    """N1: submitting with no movement chosen must not consume the parse.

    confirm_wod nulls wod_parsed to make a replay a no-op. If that ran for a
    submission that writes nothing, the session would be left with no entries and
    no way back — GET /training/wod/confirm/{id} 303s away once wod_parsed is
    NULL. The blank seed row made that one click away: open the confirm screen
    after a failed parse, press "Zapisz wpisy", lose the session for good.
    """
    _stub_llm(monkeypatch, exc=ValueError("provider unavailable"))
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": "2026-07-28", "wod_text": "ZZ blank submit probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]
    assert _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]

    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "",
            "entry_0_set_number": "1",
            "entry_0_reps": "12",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    still_pending = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert still_pending, "a submission that writes nothing must not consume the pending parse"

    # And the screen must still be reachable, not 303 away.
    assert auth_client.get(f"/training/wod/confirm/{session_id}").status_code == 200


def test_seed_row_field_names_match_what_the_route_reads(auth_client, monkeypatch):
    """Couples the seeded row's field names to the POST the route actually parses.

    The sibling save-test hand-writes its body, so renaming a seed field to an
    index the route never reads left the suite green — a user typing weight into
    `entry_9_weight` would save an entry with NULL weight. This extracts the
    names from the rendered form instead of restating them.
    """
    import re

    _stub_llm(monkeypatch, exc=ValueError("provider unavailable"))
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={"date": "2026-07-27", "wod_text": "ZZ seed field names", "_csrf_token": token},
    )
    form_html = resp.text[resp.text.index('action="/training/wod/confirm"') :]
    form_html = form_html[: form_html.index("</form>")]
    names = set(re.findall(r'name="(entry_0_[a-z_]+)"', form_html))

    expected = {
        "entry_0_movement",
        "entry_0_set_number",
        "entry_0_reps",
        "entry_0_weight",
        "entry_0_duration",
        "entry_0_note",
    }
    assert names == expected, f"seeded row must expose exactly the fields confirm_wod reads, got {sorted(names)}"


def test_no_seed_row_when_the_movement_list_is_unavailable(auth_client):
    """N3: under library_error the vocabulary is empty, so a seeded row would
    render a select whose only option is "— pomiń" — a manual-entry path that
    cannot write anything, advertised by prose promising it can.

    Uses the same over-bound trigger as the degraded-GET test above: the
    MAX_LIBRARY_MOVEMENTS assert in canonical_movements() is what empties
    `movements` on this screen.
    """
    conn = sqlite3.connect(user_db_path())
    session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 60, ?, ?)",
            ("2026-07-26", "ZZ no-seed-under-library-error", '{"entries": [], "unmatched": [], "parse_error": "boom"}'),
        )
        conn.commit()
        session_id = cur.lastrowid

        max_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) FROM exercise_library").fetchone()[0]
        conn.executemany(
            "INSERT INTO exercise_library (section, name, display_order, metric, builtin) "
            "VALUES ('Core', ?, ?, 'reps', 0)",
            [(f"NoSeed Bound Movement {i}", max_order + i + 1) for i in range(500)],
        )
        conn.commit()

        resp = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
        assert resp.status_code == 200
        assert 'name="entry_0_movement"' not in resp.text, (
            "no seed row when there is no vocabulary to pick from — it could not save anything"
        )
        assert "lista ruchów" in resp.text, "the page must say why manual entry is unavailable"
        assert "pusty wiersz do ręcznego wpisania" not in resp.text, (
            "the prose must not promise a manual-entry row that was not rendered"
        )
        # The parse_error paragraph made the same promise independently and this
        # test walked straight past it, checking only the newer sentence.
        assert "w tabeli poniżej" not in resp.text, "no paragraph may point at a table that did not render"
        assert 'action="/training/wod/confirm"' not in resp.text, "no form renders when there is nothing to pick"
    finally:
        conn.execute("DELETE FROM exercise_library WHERE name LIKE 'NoSeed Bound Movement %'")
        if session_id is not None:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()


def test_named_but_unresolvable_movement_does_not_strand_the_session(auth_client, monkeypatch):
    """S1, third attempt: the guard must be the postcondition, not a proxy.

    Two earlier fixes checked "does any row name a movement" before consuming the
    parse. The write path checks something else — resolve_movement() also returns
    None for a name that no longer resolves — so a named-but-unresolvable row
    passed the guard, consumed wod_parsed, wrote nothing, and stranded the
    session. Reachable by archiving a library row between rendering the form and
    submitting it: Settings in a second tab, or PATCH /api/library over MCP.

    Simulated here by naming a movement that exists in neither table, which is
    exactly what resolve_movement sees after such an archive.
    """
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": "2026-07-25", "wod_text": "ZZ unresolvable probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]

    before = _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"]
    before_parse = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert before_parse, "precondition: the session starts with a pending parse"
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "ZZ Movement That Does Not Exist Anywhere",
            "entry_0_set_number": "1",
            "entry_0_reps": "10",
            "entry_0_weight": "20",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"] == before, "nothing resolved, nothing written"

    still_pending = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert still_pending, "a submission that resolved nothing must leave the parse re-armed, not consumed"
    # Byte-equality, not truthiness: writing a placeholder back would keep the
    # session "reachable" while silently discarding every row the user reviewed.
    assert still_pending == before_parse, "the re-arm must restore the original parse, not a substitute"
    assert auth_client.get(f"/training/wod/confirm/{session_id}").status_code == 200, "screen must stay reachable"


def test_discard_is_a_supported_exit(auth_client, monkeypatch):
    """ "I reviewed this and want none of it" must be expressible.

    Without an explicit discard, that intent is byte-identical to "nothing
    resolved" — and one of the two has to be refused. Discard consumes the parse
    deliberately and writes nothing.
    """
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": "2026-07-24", "wod_text": "ZZ discard probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]
    before = _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"]

    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "action": "discard",
            "entry_0_movement": "",
            "entry_0_set_number": "1",
            "entry_0_reps": "",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/training", "discard is a terminal action, not a loop back to the form"
    assert _query("SELECT COUNT(*) AS n FROM training_entries")[0]["n"] == before
    pending = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert pending is None, "discard must consume the parse — that is the point"
    assert _query("SELECT notes FROM training_sessions WHERE id = ?", (session_id,))[0]["notes"], "note survives"


def test_discard_button_is_offered_on_the_confirm_screen(auth_client, monkeypatch):
    """The route accepting action=discard is useless if no UI sends it."""
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={"date": "2026-07-23", "wod_text": "ZZ discard button probe", "_csrf_token": token},
    )
    assert 'name="action" value="discard"' in resp.text


def test_pending_session_is_linked_from_the_training_page(auth_client, monkeypatch):
    """S4: the confirm screen told the user to come back later, but /training
    rendered no route to it — recovery depended on browser history."""
    _stub_llm(monkeypatch, exc=ValueError("provider unavailable"))
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": date.today().isoformat(), "wod_text": "ZZ pending link probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]

    page = auth_client.get("/training").text
    assert f"/training/wod/confirm/{session_id}" in page, (
        "a session with a pending parse must be reachable from /training"
    )

    # Negative control: without it, a link rendered for EVERY session satisfies
    # the assertion above just as well — and on a settled session that link only
    # 303s back to this page.
    # From the page's OWN window: /training renders ORDER BY date DESC LIMIT 20,
    # while this used to pick by id DESC. The two sets need not intersect, so the
    # "not in page" assertion below could pass without proving anything.
    settled = _query(
        "SELECT id FROM training_sessions WHERE wod_parsed IS NULL "
        "AND id IN (SELECT id FROM training_sessions ORDER BY date DESC LIMIT 20) "
        "ORDER BY date DESC LIMIT 1"
    )
    assert settled, "precondition: at least one settled session must exist for the control to mean anything"
    assert f"/training/wod/confirm/{settled[0]['id']}" not in page, (
        "a session with no pending parse must not be offered a 'dokończ' link"
    )


def test_library_error_with_parsed_rows_is_escapable(auth_client):
    """S2: with rows to confirm but no vocabulary, every select holds only skip
    options, so "pick a movement" is an instruction the user cannot follow. That
    state must still have an exit — discard — rather than looping on the error.
    """
    conn = sqlite3.connect(user_db_path())
    session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 60, ?, ?)",
            (
                "2026-07-22",
                "ZZ library-error escapable",
                '{"entries": [], "unmatched": ["Devil Press"], "parse_error": ""}',
            ),
        )
        conn.commit()
        session_id = cur.lastrowid
        max_order = conn.execute("SELECT COALESCE(MAX(display_order), 0) FROM exercise_library").fetchone()[0]
        conn.executemany(
            "INSERT INTO exercise_library (section, name, display_order, metric, builtin) "
            "VALUES ('Core', ?, ?, 'reps', 0)",
            [(f"Escapable Bound Movement {i}", max_order + i + 1) for i in range(500)],
        )
        conn.commit()

        page = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
        assert page.status_code == 200
        assert 'name="action" value="discard"' in page.text, "the only usable exit must be offered"

        token = csrf_token(auth_client, "/training")
        resp = auth_client.post(
            "/training/wod/confirm",
            data={
                "_csrf_token": token,
                "session_id": str(session_id),
                "entry_count": "1",
                "action": "discard",
                "entry_0_movement": "",
                "entry_0_set_number": "1",
                "entry_0_reps": "",
                "entry_0_weight": "",
                "entry_0_duration": "",
                "entry_0_note": "",
            },
            follow_redirects=False,
        )
        assert resp.headers["location"] == "/training", "discard must terminate, not loop"
    finally:
        conn.execute("DELETE FROM exercise_library WHERE name LIKE 'Escapable Bound Movement %'")
        if session_id is not None:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
        conn.commit()
        conn.close()


def test_discard_works_when_the_parse_holds_an_out_of_range_value(auth_client, monkeypatch):
    """S1 (round 4): the escape hatch must not be gated on validating the rows
    it exists to throw away.

    wod_parser applies no upper bound to reps/weight/duration — only confirm_wod
    does. So an LLM reading "row 5000m" as `reps: 5000` puts a value past
    REPS_MAX into wod_parsed, the GET renders it straight back into the form, and
    every submission bounces off _confirm_int. Discard used to sit after that
    loop, so the worse the parse, the more certainly the exit was unavailable.
    `formnovalidate` on the button suppresses the browser's check, not this one.
    """
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": 5000, "weight": None, "duration": None, "note": ""}
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": "2026-07-21", "wod_text": "ZZ out-of-range discard probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]

    # Control: saving really is refused for this parse — otherwise the test
    # would prove nothing about discard being reachable when saving is not.
    blocked = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "5000",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert "err=" in blocked.headers["location"], "control: an out-of-range reps value must be refused on save"

    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": token,
            "session_id": str(session_id),
            "entry_count": "1",
            "action": "discard",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "5000",
            "entry_0_weight": "",
            "entry_0_duration": "",
            "entry_0_note": "",
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/training", "discard must not be blocked by the rows it discards"
    pending = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert pending is None, "discard must consume the parse"


def test_entry_count_is_server_rendered_not_javascript_only(auth_client, monkeypatch):
    """The sole write path must not depend on a CDN script having loaded.

    entry_count carried only Alpine's `:value` binding, so the field existed only
    if a third-party script ran. That was survivable while a plain-HTML log form
    also existed; this route is now the only writer of training_entries, so a CDN
    failure meant no way to record a workout at all.
    """
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": 21, "weight": 43.0, "duration": None, "note": ""}
            ],
            "unmatched": [],
        },
    )
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/wod",
        data={"date": "2026-07-20", "wod_text": "ZZ static entry_count probe", "_csrf_token": token},
    )
    assert 'name="entry_count"' in resp.text
    assert re.search(r'name="entry_count"[^>]*\svalue="\d+"', resp.text), (
        "entry_count must carry a server-rendered value, not only an Alpine binding"
    )


def test_missing_entry_count_reports_the_right_cause(auth_client, monkeypatch):
    """A blank entry_count used to say "too many entries at once — try again",
    sending the user after a row-count problem they do not have and telling them
    to retry something that cannot succeed."""
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": "2026-07-19", "wod_text": "ZZ blank entry_count probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]
    resp = auth_client.post(
        "/training/wod/confirm",
        data={"_csrf_token": token, "session_id": str(session_id), "entry_count": ""},
        follow_redirects=False,
    )
    location = unquote(resp.headers["location"])
    assert "Zbyt dużo wpisów" not in location, "a missing field is not a too-many-rows problem"
    assert "niekompletny" in location
    pending = _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"]
    assert pending, "a rejected submission must leave the parse armed"


def test_discard_on_a_settled_session_is_a_logged_no_op(auth_client, monkeypatch, caplog):
    """Discard is conditioned like the consume, and reports when it matched nothing.

    An earlier version of this test claimed the condition stopped a stale tab
    from clobbering a *different* session. That was wrong: `WHERE id = ?` already
    scopes to one row, so removing `AND wod_parsed IS NOT NULL` changed no
    observable behaviour and the test could not fail — a mutation proved it.

    What the condition actually buys is the rowcount signal: "discarded a pending
    parse" and "matched nothing" stop being indistinguishable successes. That is
    what this pins.
    """
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["Devil Press"]})
    token = csrf_token(auth_client, "/training")
    auth_client.post(
        "/training/wod",
        data={"date": date.today().isoformat(), "wod_text": "ZZ discard no-op probe", "_csrf_token": token},
    )
    session_id = _sessions()[0]["id"]

    def discard():
        return auth_client.post(
            "/training/wod/confirm",
            data={"_csrf_token": token, "session_id": str(session_id), "entry_count": "0", "action": "discard"},
            follow_redirects=False,
        )

    # First discard: a real one. Must not log a no-match.
    with caplog.at_level("WARNING", logger="app.routers.training"):
        caplog.clear()
        assert discard().headers["location"] == "/training"
        assert "matched nothing" not in caplog.text, "a discard that consumed a parse is not a no-match"
    assert _query("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"] is None

    # Replay against the now-settled session: same 303, but reported as a no-match.
    with caplog.at_level("WARNING", logger="app.routers.training"):
        caplog.clear()
        assert discard().headers["location"] == "/training"
        assert "matched nothing" in caplog.text, (
            "discarding an already-settled session must be distinguishable from discarding a pending parse"
        )


@pytest.mark.parametrize("stored", ["not-json{{{", "null", "[]", "123", '"x"'])
def test_unusable_wod_parsed_is_cleared_instead_of_500ing_or_sticking(auth_client, stored):
    """Valid JSON that is not an object reached parsed.get(...) as an
    AttributeError — a permanent 500 on the GET the corrupt-JSON guard exists to
    keep reachable, now also offered as a clickable 'dokończ' link.

    Clearing rather than redirecting: an unreadable parse carries nothing, but
    leaving it armed kept the session flagged forever behind a link that only
    redirects back, with deleting the session (and its note) the only escape.
    """
    conn = sqlite3.connect(user_db_path())
    session_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, wod_parsed) VALUES (?, 30, ?, ?)",
            # today(), not a fixed date: /training history is ORDER BY date DESC
            # LIMIT 20, so a past date falls out of the window as soon as other
            # tests leave later sessions behind — and the precondition below
            # depends on this session being rendered.
            (date.today().isoformat(), "ZZ unusable parse", stored),
        )
        conn.commit()
        session_id = cur.lastrowid

        assert f"/training/wod/confirm/{session_id}" in auth_client.get("/training").text, (
            "precondition: an armed session is linked, which is what makes this state clickable"
        )

        resp = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
        assert resp.status_code == 303, f"{stored!r} must not 500"

        conn2 = sqlite3.connect(user_db_path())
        try:
            row = conn2.execute(
                "SELECT wod_parsed, notes FROM training_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn2.close()
        assert row[0] is None, "an unusable parse must be cleared, not left armed forever"
        assert row[1] == "ZZ unusable parse", "the raw note must survive"
        assert f"/training/wod/confirm/{session_id}" not in auth_client.get("/training").text
    finally:
        if session_id is not None:
            conn = sqlite3.connect(user_db_path())
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
            conn.commit()
            conn.close()
