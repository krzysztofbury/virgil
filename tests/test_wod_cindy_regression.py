"""The reported session, end to end through the real HTTP route.

The note the user actually typed:

    crossfit:
    warmup: 3 minutes rowing + stretching
    weightlifting: high hang snatch 6 series 2 reps each max 15kg + 15 bar
    workout "cindy": amrap 20 minutes, 7 series, 5x pull up, 10x push ups, 15 squats

It produced no usable rows at all. Two independent defects, both exercised here
against the real router, the real migrated vocabulary and the real template -
only `call_llm` itself is stubbed:

 1. The response was truncated (thinking ate the 4096-token budget) and
    parse_andy_response could not repair a cut three levels deep, so every
    correctly-parsed movement was discarded.
 2. "15 squats" had no canonical match, because the seeded vocabulary carried
    Back/Front/Overhead Squat but no bodyweight squat.
"""

import json
import sqlite3

import pytest
from conftest import csrf_token, drain_jobs, user_db_path

WOD_TEXT = (
    "ZZ crossfit:\n"
    "warmup: 3 minutes rowing + stretching\n"
    "weightlifting: high hang snatch 6 series 2 reps each max 15kg + 15 bar\n"
    'workout "cindy": amrap 20 minutes, 7 series, 5x pull up, 10x push ups, 15 squats'
)


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


def _cindy_entries() -> list[dict]:
    """What a correct parse of the note above yields: 28 entries.

    One row per AMRAP round, which is what the system prompt asks for and what
    makes this note the token-hungriest input the parser sees.
    """
    entries: list[dict] = [
        {"movement": "Row", "set_number": 1, "reps": None, "weight": None, "duration": 180.0, "note": "warmup"},
    ]
    entries += [
        {"movement": "Snatch", "set_number": n, "reps": 2, "weight": 30.0, "duration": None, "note": "high hang"}
        for n in range(1, 7)
    ]
    for rnd in range(1, 8):
        entries.append(
            {"movement": "Pull-up", "set_number": rnd, "reps": 5, "weight": None, "duration": None, "note": "cindy"}
        )
        entries.append(
            {"movement": "Push-up", "set_number": rnd, "reps": 10, "weight": None, "duration": None, "note": ""}
        )
        entries.append(
            {"movement": "Air Squat", "set_number": rnd, "reps": 15, "weight": None, "duration": None, "note": ""}
        )
    return entries


def _stub_raw(monkeypatch, raw: str):
    import app.services.wod_parser as wp

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        # The prompt must actually offer the movements this payload names,
        # otherwise the assertions below would pass against a stub that has
        # drifted from the real vocabulary.
        for name in ("Air Squat", "Row", "Snatch", "Push-up", "Pull-up"):
            assert f"- {name}" in system_prompt, f"{name} missing from the parser's vocabulary"
        return raw

    monkeypatch.setattr(wp, "call_llm", fake_call_llm)


def _post(auth_client):
    token = csrf_token(auth_client, "/training")
    captured = auth_client.post(
        "/training/wod",
        data={"date": "2026-08-03", "duration_minutes": "75", "wod_text": WOD_TEXT, "_csrf_token": token},
    )
    # The parse is a durable job now, so the confirm page is only settled once
    # the worker has run.
    drain_jobs()
    return auth_client.get(captured.url)


def test_complete_response_maps_the_whole_session(auth_client, monkeypatch):
    """With Air Squat in the vocabulary, nothing from this note is unmatched."""
    _stub_raw(monkeypatch, json.dumps({"entries": _cindy_entries(), "unmatched": []}))
    resp = _post(auth_client)
    assert resp.status_code == 200

    assert "Parsowanie nie powiodło się" not in resp.text
    assert "Nic nie udało się sparsować" not in resp.text
    # 28 entry rows, and the last one really is rendered (an off-by-one in the
    # template would otherwise pass every count-free assertion).
    assert 'name="entry_27_movement"' in resp.text
    assert 'name="entry_28_movement"' not in resp.text
    assert 'name="entry_count"' in resp.text
    assert 'value="28"' in resp.text

    # The bodyweight squat resolves rather than landing in `unmatched`.
    assert 'value="Air Squat" selected' in resp.text
    assert 'value="Row" selected' in resp.text
    # 3 minutes of rowing, stored and rendered in SECONDS (migration 020's unit).
    assert 'name="entry_0_duration" value="180.0"' in resp.text


def test_truncated_response_still_delivers_the_movements_that_arrived(auth_client, monkeypatch):
    """The exact prod failure: cut mid-object, three levels deep.

    Before the fix this rendered "Nic nie udało się sparsować" plus one blank
    seed row - the screenshot the bug was reported with.
    """
    full = json.dumps({"entries": _cindy_entries(), "unmatched": []}, indent=2)
    # Stop part-way through the 26th entry, immediately after its set_number -
    # where the real 837-character response stopped.
    idx = -1
    for _ in range(26):
        idx = full.index('"movement":', idx + 1)
    truncated = full[: full.index("\n", full.index('"set_number":', idx))]
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)

    _stub_raw(monkeypatch, truncated)
    resp = _post(auth_client)
    assert resp.status_code == 200

    assert "Nic nie udało się sparsować" not in resp.text, "the truncated tail must not cost the whole session"
    assert "Parsowanie nie powiodło się" not in resp.text
    # The 25 complete entries plus the half-written 26th.
    assert 'name="entry_25_movement"' in resp.text
    assert 'name="entry_26_movement"' not in resp.text
    assert 'value="Air Squat" selected' in resp.text
    assert 'name="entry_0_duration" value="180.0"' in resp.text

    # The raw note is intact regardless - that guarantee predates this fix.
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT notes, wod_parsed FROM training_sessions ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row["notes"] == WOD_TEXT
    stored = json.loads(row["wod_parsed"])
    assert stored["parse_error"] == "", "a repaired parse is not a failed parse"
    assert len(stored["entries"]) == 26
