"""POST /training/wod — the note is persisted before the LLM is ever called."""

import json
import sqlite3

import pytest
from conftest import csrf_token, user_db_path


def _sessions():
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM training_sessions ORDER BY id DESC")]
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
    """The INSERT+commit must precede parse_wod. A ValueError is caught by the
    route and proves nothing about ordering (the commit already happened either
    way by the time we inspect the response). An exception the route does NOT
    catch is the only thing that can tell the two orderings apart: if the insert
    were moved after the parse call, this crash would take the session with it.

    Not hypothetical: call_llm can raise non-ValueError — the bare asserts in
    app/services/llm.py (max_tokens bounds, missing content), transport errors
    that aren't litellm.APIError subclasses, and asyncio.CancelledError on
    client disconnect.
    """
    import app.services.wod_parser as wp

    async def boom(db, system_prompt, user_prompt, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(wp, "call_llm", boom)
    token = csrf_token(auth_client, "/training")
    with pytest.raises(RuntimeError):
        auth_client.post(
            "/training/wod",
            data={
                "date": "2026-07-30",
                "duration_minutes": "50",
                "wod_text": "5 rund: 10 burpee, 15 wall ball",
                "_csrf_token": token,
            },
        )
    assert _sessions()[0]["notes"] == "5 rund: 10 burpee, 15 wall ball"


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


def test_unmatched_row_skipped_on_confirm_creates_no_entry(auth_client, monkeypatch):
    """Submitting the confirm form with the unmatched row left on '— pomiń —'
    (empty movement value) must create no training_entries row for it."""
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
