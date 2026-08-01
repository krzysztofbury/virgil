"""The training page is free-text capture plus read-only history — nothing else.

Replaces the previous suites for this page (a protocol CRUD table, a per-set log
form, an exercise picker and a rest timer). Those surfaces are gone, so tests
that drove them were deleted rather than adapted; what survives here is the
contract that replaced them.

Every "X is absent" assertion is paired with a positive one on the same
response. An absence check alone passes just as happily when the page 500s or
renders empty, which is the failure it is supposed to catch.
"""

import sqlite3
from datetime import date

from conftest import csrf_token, user_db_path


def _page(auth_client) -> str:
    resp = auth_client.get("/training")
    assert resp.status_code == 200, f"training page must render, got {resp.status_code}"
    return resp.text


def test_capture_form_is_the_only_input(auth_client):
    html = _page(auth_client)

    assert 'action="/training/wod"' in html, "the capture form is the page's one input"
    assert 'name="wod_text"' in html

    assert "Training Protocol" not in html, "the protocol table was removed"
    assert "Log Workout" not in html, "the per-set log form was removed"
    assert "Rest Timer" not in html, "the rest timer was removed"
    assert 'action="/training/exercise"' not in html, "protocol CRUD was removed"


def test_kpis_and_history_survive(auth_client):
    """Creates its own session rather than relying on one being there.

    `auth_client` is session-scoped and the DB is shared across test files, so
    the delete-form assertion below passed only because an earlier file happened
    to leave a row behind — green in a full run, red when this file runs alone.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 42, 'ZZTestPageSession')",
            # Today, not a hardcoded date: history renders ORDER BY date DESC
            # LIMIT 20, so a fixed past date drops off the page once enough
            # later sessions exist.
            (date.today().isoformat(),),
        )
        session_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    try:
        html = _page(auth_client)
        assert "This Week" in html
        assert "Workout History" in html
        assert f'action="/training/session/{session_id}/delete"' in html, "history keeps its per-session delete"
    finally:
        conn = sqlite3.connect(user_db_path())
        try:
            conn.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def test_no_picker_payload_is_embedded(auth_client):
    """The page used to embed every library row as JSON to feed the picker.

    Asserted through a positive control: the library is still populated (so a
    passing test means the payload is genuinely gone, not that the library
    happens to be empty).
    """
    conn = sqlite3.connect(user_db_path())
    try:
        lib_count = conn.execute("SELECT COUNT(*) FROM exercise_library WHERE archived = 0").fetchone()[0]
    finally:
        conn.close()
    assert lib_count > 0, "precondition: the library must be non-empty for this test to mean anything"

    html = _page(auth_client)
    assert "exercisePicker(" not in html, "the picker and its JSON payload were removed"
    assert "data-tag-filter" not in html, "tag filter chips lived in the picker"


def test_removed_write_endpoints_are_unroutable(auth_client):
    """The deleted endpoints must be gone from the router, not merely unlinked.

    A form removed from a template while its POST handler survives is still a
    live write path — reachable by anything holding the old URL.
    """
    token = csrf_token(auth_client, "/training")
    for path in (
        "/training/session",
        "/training/exercise",
        "/training/exercise/1/edit",
        "/training/exercise/1/delete",
    ):
        resp = auth_client.post(path, data={"_csrf_token": token}, follow_redirects=False)
        assert resp.status_code == 404, f"{path} must be unroutable, got {resp.status_code}"


def test_session_delete_still_routes(auth_client):
    """Control for the test above: a path that IS still registered must not 404,
    so a blanket 404 (bad prefix, auth redirect) cannot make that test pass."""
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post("/training/session/999999999/delete", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code != 404, "the surviving delete route must still be registered"
