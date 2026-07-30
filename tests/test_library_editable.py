"""CrossFit library rows must be editable and deletable by the user.

settings.py guards update/delete with `AND builtin = 0`, so a builtin=1 row is
silently un-editable — the route returns 303 either way and the row is unchanged.
That silence is why these assert on the DB, never on the response code.
"""

import sqlite3

from conftest import csrf_token, user_db_path


def _row(name: str) -> dict | None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM exercise_library WHERE category = 'CrossFit' AND name = ?", (name,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def test_crossfit_rows_are_not_builtin(auth_client):
    row = _row("Thruster")
    assert row is not None, "migration 016 must have seeded Thruster"
    assert row["builtin"] == 0, "builtin=1 makes the row un-editable and un-deletable"


def test_crossfit_row_can_be_edited(auth_client):
    row = _row("Thruster")
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(row["id"]),
            "name": "Thruster",
            "section": "Core",
            "sets": "",
            "reps": "21-15-9",
            "notes": "front rack to overhead",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    after = _row("Thruster")
    assert after["notes"] == "front rack to overhead", "edit was silently dropped by the builtin guard"
    assert after["reps"] == "21-15-9"


def test_crossfit_row_can_be_deleted(auth_client):
    row = _row("Single-under")
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/delete",
        data={"entry_id": str(row["id"]), "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _row("Single-under") is None, "delete was silently dropped by the builtin guard"
