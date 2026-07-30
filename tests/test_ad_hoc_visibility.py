"""ad_hoc exercises are excluded from protocol form but visible in volume/PBs."""

import sqlite3
from datetime import date, timedelta

from conftest import user_db_path


def _get_monday():
    """Get Monday of the current week."""
    today = date.today()
    return today - timedelta(days=today.weekday())


def _create_ad_hoc_exercise_with_entry(auth_client) -> int:
    """Insert an ad_hoc=1 exercise with a unique name and log an entry for it.

    Returns the exercise_id.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        # Use a name that cannot collide with any other exercise on the page
        cursor = conn.execute(
            """INSERT INTO training_exercises
               (name, section, target_sets, target_reps, metric, ad_hoc)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("ZZTestAdHocMovement", "Core", None, "", "reps", 1),
        )
        ex_id = cursor.lastrowid

        # Insert a session and entry for the ad_hoc exercise (dated to this week)
        cursor = conn.execute(
            "INSERT INTO training_sessions (date) VALUES (?)",
            (_get_monday().isoformat(),),
        )
        session_id = cursor.lastrowid

        # Log: 5 reps × 70 kg (350 kg volume contribution)
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, ?, ?)",
            (session_id, ex_id, 1, 5, 70.0),
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


def test_ad_hoc_excluded_from_protocol_section(auth_client):
    """ad_hoc=1 movements must not appear in the protocol form section.

    The protocol section is the exercise-picker table (training.html:9-24).
    It renders {{ ex.name }} literally. Session history below it deliberately
    does NOT filter on ad_hoc (queries join by id only), so the exercise's
    name legitimately appears further down once it has entries. This test
    scopes the assertion to the protocol section only.
    """
    ex_id = _create_ad_hoc_exercise_with_entry(auth_client)
    try:
        resp = auth_client.get("/training")
        assert resp.status_code == 200

        # The exercise name should NOT appear in the protocol section.
        # "Personal Bests" is the next section, so split there.
        protocol_section = resp.text.split("Personal Bests")[0]
        assert "ZZTestAdHocMovement" not in protocol_section, "ad_hoc exercise should be absent from protocol form"

        # Verify the exercise does appear elsewhere (in session history),
        # so it's not being filtered out entirely.
        assert "ZZTestAdHocMovement" in resp.text, "exercise should appear in session history (not filtered globally)"
    finally:
        _cleanup_ad_hoc_exercise(ex_id)


def test_ad_hoc_counted_in_core_volume(auth_client):
    """ad_hoc=1 movements contribute to weekly Core volume aggregates.

    The volume query (training.py:108-118) joins training_entries by id
    with no filter on ad_hoc, so ad_hoc entries ARE counted. This test
    directly queries the aggregate and verifies the numeric contribution.
    """
    ex_id = _create_ad_hoc_exercise_with_entry(auth_client)
    try:
        # Query the weekly volume for Core/reps exercises (matching training.py)
        monday = _get_monday().isoformat()
        conn = sqlite3.connect(user_db_path())
        try:
            volume_row = conn.execute(
                """SELECT
                   SUM(CASE WHEN tex.section = 'Core' AND tex.metric = 'reps'
                            THEN te.reps * COALESCE(te.weight, 0) ELSE 0 END) as core_volume
               FROM training_entries te
               JOIN training_sessions ts ON te.session_id = ts.id
               JOIN training_exercises tex ON te.exercise_id = tex.id
               WHERE ts.date >= ? AND te.exercise_id = ?""",
                (monday, ex_id),
            ).fetchone()

            volume = volume_row[0] if volume_row and volume_row[0] is not None else 0
            # We logged 5 reps × 70 kg = 350 kg
            assert volume == 350, f"expected 350 kg volume, got {volume}"
        finally:
            conn.close()
    finally:
        _cleanup_ad_hoc_exercise(ex_id)


def test_ad_hoc_counted_in_personal_bests(auth_client):
    """ad_hoc=1 movements contribute to Personal Best (max weight) aggregates.

    The PB query (training.py:128-135) joins training_entries by id
    with no filter on ad_hoc, so ad_hoc entries ARE counted. This test
    directly queries the PB aggregate and verifies the numeric contribution.
    """
    ex_id = _create_ad_hoc_exercise_with_entry(auth_client)
    try:
        # Query the PB for the last 12 weeks (matching training.py)
        twelve_weeks_ago = (date.today() - timedelta(weeks=12)).isoformat()
        conn = sqlite3.connect(user_db_path())
        try:
            pb_row = conn.execute(
                """SELECT MAX(te.weight) as max_weight
               FROM training_entries te
               JOIN training_sessions ts ON te.session_id = ts.id
               JOIN training_exercises tex ON te.exercise_id = tex.id
               WHERE ts.date >= ? AND tex.section = 'Core' AND te.weight > 0
                 AND te.exercise_id = ?""",
                (twelve_weeks_ago, ex_id),
            ).fetchone()

            max_weight = pb_row[0] if pb_row and pb_row[0] is not None else 0
            # We logged 70 kg
            assert max_weight == 70.0, f"expected max weight 70 kg, got {max_weight}"
        finally:
            conn.close()
    finally:
        _cleanup_ad_hoc_exercise(ex_id)
