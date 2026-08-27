"""Writing 1 of 2 rows must be reported, not silent.

confirm_wod skips a row whose movement does not resolve, and the `if not rows`
guard is all-or-nothing. So a submission could write one row, drop the other,
and redirect with a success page and no message at all.
"""

import json
import sqlite3
from urllib.parse import unquote

from conftest import csrf_token, user_db_path


def _new_session():
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, notes, wod_parsed) VALUES ('2026-08-26', 'partial probe', ?)",
            (json.dumps({"entries": [], "unmatched": [], "parse_error": ""}),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _entry_count(session_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT COUNT(*) FROM training_entries WHERE session_id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def test_partial_resolve_names_the_skipped_movement(auth_client):
    """ "Ghost Movement" is outside the exercise library, so resolve_movement
    returns None for it and creates nothing - the real trigger, no stubbing."""
    session_id = _new_session()
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": csrf_token(auth_client, "/training"),
            "session_id": str(session_id),
            "entry_count": "2",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "21",
            "entry_1_movement": "Ghost Movement",
            "entry_1_set_number": "1",
            "entry_1_reps": "10",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    location = unquote(resp.headers["location"])
    assert location.startswith("/training?msg="), f"a partial write must be reported: {location}"
    assert "Ghost Movement" in location, "the message must name what was skipped"
    assert _entry_count(session_id) == 1, "the resolved row must still be written"


def test_deliberate_skip_is_silent(auth_client):
    """A blank movement is the "- pomiń" option. Reporting it would be noise."""
    session_id = _new_session()
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": csrf_token(auth_client, "/training"),
            "session_id": str(session_id),
            "entry_count": "2",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "21",
            "entry_1_movement": "",
            "entry_1_set_number": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/training"
    assert _entry_count(session_id) == 1


def test_training_page_renders_the_message(auth_client):
    html = auth_client.get("/training?msg=zapisano%201%20z%202").text
    assert "zapisano 1 z 2" in html
