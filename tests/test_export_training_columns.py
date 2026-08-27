"""The markdown export must carry every column the confirm screen collects.

`duration` had just gained a canonical unit (seconds, migration 020) and the
export was the one reader that dropped it. `notes` holds the metcon result the
parser prompt designates, and it had no reader at all.
"""

import asyncio
import sqlite3

import aiosqlite
from conftest import user_db_path

from app.services.markdown_export import _section_training

EXPORT_DATE = "2026-08-20"


def _seed_session():
    conn = sqlite3.connect(user_db_path())
    try:
        session_id = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, 45, 'raw note')",
            (EXPORT_DATE,),
        ).lastrowid
        exercise_id = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, display_order) "
            "VALUES ('Export Probe', 'Core', 'reps', 300)"
        ).lastrowid
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration, notes) "
            "VALUES (?, ?, 1, 21, 43.0, 522, '8:42 RX')",
            (session_id, exercise_id),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def _run_section(date_str):
    async def run():
        async with aiosqlite.connect(user_db_path()) as db:
            db.row_factory = aiosqlite.Row
            return await _section_training(db, date_str, date_str)

    return "\n".join(asyncio.run(run()))


def test_export_carries_duration_and_notes(auth_client):
    _seed_session()
    text = _run_section(EXPORT_DATE)
    assert "| Exercise | Set | Reps | Weight | Duration | Notes |" in text
    # format_duration_seconds renders minutes plus seconds, never a clock string.
    assert "8 min 42 s" in text
    assert "8:42 RX" in text
