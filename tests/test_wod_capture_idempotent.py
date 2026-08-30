"""A replayed capture must not create a second session or a second paid parse.

POST /training/wod commits the session before it calls the LLM, by design: a
parse failure must never cost the raw note. That made a double click create two
sessions and pay for two parses, and the first session then counted toward the
weekly KPI with no entries.
"""

import re
import sqlite3
from urllib.parse import urlsplit

from conftest import csrf_token, drain_jobs, user_db_path

from app.services.wod_parser import ParsedWod

TOKEN_FIELD = re.compile(r'name="capture_token" value="([0-9a-f]{32})"')


def _count_sessions(note):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT COUNT(*) FROM training_sessions WHERE notes = ?", (note,)).fetchone()[0]
    finally:
        conn.close()


def _session_row(note):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT id, capture_token FROM training_sessions WHERE notes = ?", (note,)).fetchone()
    finally:
        conn.close()


def _stub_parse(monkeypatch, calls):
    """The parse now runs in the worker, so the stub belongs at its own module."""

    async def fake_parse(db, text):
        calls.append(text)
        return ParsedWod(entries=[], unmatched=[], dropped=0)

    monkeypatch.setattr("app.services.wod_parser.parse_wod", fake_parse)


def test_same_capture_token_creates_one_session(auth_client, monkeypatch):
    calls = []
    _stub_parse(monkeypatch, calls)

    payload = {
        "_csrf_token": csrf_token(auth_client, "/training"),
        "date": "2026-08-25",
        "wod_text": "double click probe",
        "capture_token": "probe-token-0001",
    }
    first = auth_client.post("/training/wod", data=payload, follow_redirects=False)
    drain_jobs()
    second = auth_client.post("/training/wod", data=payload, follow_redirects=False)
    drain_jobs()

    assert first.status_code == 303
    assert second.status_code == 303
    # Same confirm screen. The feedback text differs: the first request queued
    # the parse, the second recognised a replay of it.
    assert urlsplit(first.headers["location"]).path == urlsplit(second.headers["location"]).path
    assert _count_sessions("double click probe") == 1
    assert len(calls) == 1, "the second request paid for a second parse"


def test_reused_token_with_a_new_note_still_saves_it(auth_client, monkeypatch):
    """The back button restores the page and its token. A new note must survive.

    Treating this as a replay redirected the user to another session's confirm
    screen and dropped the note they had just written, silently.
    """
    calls = []
    _stub_parse(monkeypatch, calls)

    base = {
        "_csrf_token": csrf_token(auth_client, "/training"),
        "date": "2026-08-25",
        "capture_token": "reused-token-0002",
    }
    auth_client.post("/training/wod", data={**base, "wod_text": "first note"}, follow_redirects=False)
    drain_jobs()
    second = auth_client.post("/training/wod", data={**base, "wod_text": "second note"}, follow_redirects=False)
    drain_jobs()

    assert second.status_code == 303
    assert _count_sessions("second note") == 1, "the second note was dropped as a false replay"
    row = _session_row("second note")
    assert row[1] is None, "a fresh session must not keep the colliding token"
    assert urlsplit(second.headers["location"]).path == f"/training/wod/confirm/{row[0]}"
    assert len(calls) == 2, "a genuine new capture must still be parsed"


def test_training_page_mints_a_fresh_capture_token(auth_client):
    first = TOKEN_FIELD.search(auth_client.get("/training").text)
    second = TOKEN_FIELD.search(auth_client.get("/training").text)
    assert first and second, "capture_token field missing from the capture form"
    assert first.group(1) != second.group(1)
