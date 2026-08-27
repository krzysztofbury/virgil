"""A rejected field must not cost the user the rest of the form.

The route used to answer _ConfirmRejected with a 303 to its own GET, which
rebuilt the form from the STORED parse. Every other edit went with it, rows the
user had added included, and the message named one field so the loss was
invisible until they looked.
"""

import json
import sqlite3

from conftest import csrf_token, user_db_path


def _new_session(parsed=None):
    parsed = parsed or {"entries": [], "unmatched": [], "parse_error": ""}
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, notes, wod_parsed) VALUES ('2026-08-26', 'rerender probe', ?)",
            (json.dumps(parsed),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _pending(session_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def _entry_count(session_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT COUNT(*) FROM training_entries WHERE session_id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def test_rejected_row_rerenders_every_submitted_value(auth_client):
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
            "entry_0_weight": "43",
            "entry_0_duration": "",
            "entry_0_note": "21-15-9",
            "entry_1_movement": "Thruster",
            "entry_1_set_number": "2",
            "entry_1_reps": "99999",
            "entry_1_weight": "",
            "entry_1_duration": "",
            "entry_1_note": "keep me",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, "a rejected submission must re-render, not redirect to a clean form"
    assert 'value="21"' in resp.text, "row 0 reps were dropped"
    assert 'value="21-15-9"' in resp.text, "row 0 note was dropped"
    assert 'value="43"' in resp.text, "row 0 weight was dropped"
    assert 'value="99999"' in resp.text, "the rejected value must stay visible"
    assert 'value="keep me"' in resp.text, "row 1 note was dropped"
    assert 'value="Thruster" selected' in resp.text, "the chosen movement was dropped"
    assert "powtórzenia" in resp.text, "the message must name the field"
    assert "wpis 2" in resp.text, "the message must name the row"
    assert _pending(session_id) is not None, "a rejected submission must not consume the parse"
    assert _entry_count(session_id) == 0, "a rejected submission must write nothing"


def test_rerendered_form_can_be_submitted_again(auth_client):
    """The re-render must carry a usable CSRF token and session_id, or the retry
    the message invites cannot work."""
    session_id = _new_session()
    rejected = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": csrf_token(auth_client, "/training"),
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "99999",
        },
        follow_redirects=False,
    )
    assert rejected.status_code == 200
    assert f'name="session_id" value="{session_id}"' in rejected.text
    assert 'name="entry_count" x-ref="entryCount"' in rejected.text

    retry = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": csrf_token(auth_client, "/training"),
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "21",
        },
        follow_redirects=False,
    )
    assert retry.status_code == 303
    assert _entry_count(session_id) == 1
    assert _pending(session_id) is None, "the successful retry consumes the parse"


def test_rejected_non_numeric_value_comes_back_as_typed(auth_client):
    """The user must see what the message is about, so the raw string returns."""
    session_id = _new_session()
    resp = auth_client.post(
        "/training/wod/confirm",
        data={
            "_csrf_token": csrf_token(auth_client, "/training"),
            "session_id": str(session_id),
            "entry_count": "1",
            "entry_0_movement": "Thruster",
            "entry_0_set_number": "1",
            "entry_0_reps": "dwadzieścia jeden",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert 'value="dwadzieścia jeden"' in resp.text
    assert "nie jest liczbą całkowitą" in resp.text
