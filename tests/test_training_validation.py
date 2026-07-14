"""Training input safety: no empty sessions, no negative values, archive-not-delete."""

import sqlite3

from conftest import csrf_token, user_db_path


def _count(table: str) -> int:
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        conn.close()


def _seeded_core_exercise_id() -> int:
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(
            "SELECT id FROM training_exercises WHERE section = 'Core' AND metric = 'reps' AND archived = 0 LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()


def test_empty_workout_creates_no_session(auth_client):
    before = _count("training_sessions")
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/session",
        data={"date": "2026-07-07", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _count("training_sessions") == before


def test_negative_reps_are_rejected(auth_client):
    ex_id = _seeded_core_exercise_id()
    before = _count("training_sessions")
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/session",
        data={
            "date": "2026-07-07",
            f"exercise_{ex_id}_set_1_reps": "-5",
            f"exercise_{ex_id}_set_1_weight": "-20",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # The only entry was invalid → no session at all.
    assert _count("training_sessions") == before


def test_valid_workout_saves_then_exercise_archives_not_deletes(auth_client):
    ex_id = _seeded_core_exercise_id()
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/session",
        data={
            "date": "2026-07-07",
            f"exercise_{ex_id}_set_1_reps": "10",
            f"exercise_{ex_id}_set_1_weight": "24",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = sqlite3.connect(user_db_path())
    try:
        entry = conn.execute(
            "SELECT reps, weight FROM training_entries WHERE exercise_id = ? ORDER BY id DESC LIMIT 1", (ex_id,)
        ).fetchone()
        assert entry == (10, 24.0)
    finally:
        conn.close()

    # Deleting an exercise WITH history must archive it and keep the entries.
    resp = auth_client.post(f"/training/exercise/{ex_id}/delete", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    conn = sqlite3.connect(user_db_path())
    try:
        archived = conn.execute("SELECT archived FROM training_exercises WHERE id = ?", (ex_id,)).fetchone()
        assert archived is not None, "Exercise row must survive deletion when it has history"
        assert archived[0] == 1
        entries = conn.execute("SELECT COUNT(*) FROM training_entries WHERE exercise_id = ?", (ex_id,)).fetchone()[0]
        assert entries > 0, "History must be preserved"
        # Cleanup: restore protocol + drop the test session.
        conn.execute("UPDATE training_exercises SET archived = 0 WHERE id = ?", (ex_id,))
        conn.execute(
            "DELETE FROM training_sessions WHERE id IN (SELECT session_id FROM training_entries WHERE exercise_id = ?)",
            (ex_id,),
        )
        conn.execute("DELETE FROM training_entries WHERE exercise_id = ?", (ex_id,))
        conn.commit()
    finally:
        conn.close()


def test_unused_exercise_hard_deletes(auth_client):
    token = csrf_token(auth_client, "/training")
    resp = auth_client.post(
        "/training/exercise",
        data={"name": "Temp Exercise", "section": "Core", "target_sets": "3", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    conn = sqlite3.connect(user_db_path())
    try:
        ex_id = conn.execute("SELECT id FROM training_exercises WHERE name = 'Temp Exercise'").fetchone()[0]
    finally:
        conn.close()

    resp = auth_client.post(f"/training/exercise/{ex_id}/delete", data={"_csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    conn = sqlite3.connect(user_db_path())
    try:
        row = conn.execute("SELECT 1 FROM training_exercises WHERE id = ?", (ex_id,)).fetchone()
        assert row is None, "Unused exercise should hard-delete"
    finally:
        conn.close()
