"""POST /training/wod — the note is persisted before the LLM is ever called."""

import json
import sqlite3

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
    assert "Thruster" in resp.text
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
