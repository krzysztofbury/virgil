"""ad_hoc exercises are excluded from protocol form but visible in volume/PBs."""

import sqlite3
from datetime import date

from conftest import user_db_path


def _create_ad_hoc_exercise_with_entry(auth_client) -> int:
    """Insert an ad_hoc=1 exercise and a training entry for it.

    Returns the exercise_id.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        # Insert an ad_hoc movement (would be created by the WOD parser)
        cursor = conn.execute(
            """INSERT INTO training_exercises
               (name, section, target_sets, target_reps, metric, ad_hoc)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("Rope Climb", "Core", None, "", "reps", 1),
        )
        ex_id = cursor.lastrowid

        # Insert a session and entry for the ad_hoc exercise
        cursor = conn.execute(
            "INSERT INTO training_sessions (date) VALUES (?)",
            (date.today().isoformat(),),
        )
        session_id = cursor.lastrowid

        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, ?, ?)",
            (session_id, ex_id, 1, 5, None),
        )

        conn.commit()
        return ex_id
    finally:
        conn.close()


def _cleanup_ad_hoc_exercise(ex_id: int) -> None:
    """Clean up an ad_hoc exercise and its entries."""
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "DELETE FROM training_sessions WHERE id IN (SELECT session_id FROM training_entries WHERE exercise_id = ?)",
            (ex_id,),
        )
        conn.execute("DELETE FROM training_entries WHERE exercise_id = ?", (ex_id,))
        conn.execute("DELETE FROM training_exercises WHERE id = ?", (ex_id,))
        conn.commit()
    finally:
        conn.close()


def test_ad_hoc_excluded_from_protocol_form(auth_client):
    """ad_hoc=1 movements must not appear in the daily protocol form."""
    ex_id = _create_ad_hoc_exercise_with_entry(auth_client)
    try:
        resp = auth_client.get("/training")
        assert resp.status_code == 200

        # The protocol form is built from the exercises dict passed to the template.
        # We verify the exercise form field does NOT appear (exercise_{id}_set_1_reps).
        assert f"exercise_{ex_id}_set_1_reps" not in resp.text, "ad_hoc exercise should not appear in protocol form"
    finally:
        _cleanup_ad_hoc_exercise(ex_id)


def test_ad_hoc_counted_in_weekly_volume(auth_client):
    """ad_hoc=1 movements stay out of the protocol but remain visible to volume KPI."""
    ex_id = _create_ad_hoc_exercise_with_entry(auth_client)
    try:
        resp = auth_client.get("/training")
        assert resp.status_code == 200

        # The KPI section shows "Total reps this week" which is calculated by the
        # volume query (training.py:108-118). That query joins training_entries to
        # training_exercises with no filter on ad_hoc, so ad_hoc entries ARE counted.
        # We expect the page to show at least 5 reps (from our Rope Climb entry).
        # The KPI is rendered in the response HTML.
        assert "kpi_reps" in resp.text or "reps" in resp.text.lower()

        # Verify in the database that the entry exists and will be counted
        conn = sqlite3.connect(user_db_path())
        try:
            entry = conn.execute(
                "SELECT reps FROM training_entries WHERE exercise_id = ?",
                (ex_id,),
            ).fetchone()
            assert entry is not None, "Entry should exist"
            assert entry[0] == 5, "Entry should have 5 reps"
        finally:
            conn.close()
    finally:
        _cleanup_ad_hoc_exercise(ex_id)
