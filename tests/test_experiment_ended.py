"""An active experiment past its last day reads as ended, not active (#95).

It stays "active" in the database until the user marks it completed; the pages
must not keep offering to log a day that is outside it.
"""

import sqlite3
from datetime import date, timedelta

import pytest

from app.services.experiment_weeks import experiment_calendar
from tests.conftest import user_db_path


def _experiment(start: date, weeks: int) -> int:
    conn = sqlite3.connect(user_db_path())
    try:
        experiment_id = conn.execute(
            "INSERT INTO experiments (title, description, start_date, num_weeks, status) VALUES (?, '', ?, ?, 'active')",
            (f"ZZ Ended check {start}", start.isoformat(), weeks),
        ).lastrowid
        conn.execute(
            "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period, "
            "display_order) VALUES (?, 'ZZ Walk', '#22c55e', 'boolean', 1, 'day', 0)",
            (experiment_id,),
        )
        conn.commit()
        return experiment_id
    finally:
        conn.close()


@pytest.fixture
def cleanup():
    created = []
    yield created
    conn = sqlite3.connect(user_db_path())
    for experiment_id in created:
        conn.execute("DELETE FROM experiment_activity_types WHERE experiment_id = ?", (experiment_id,))
        conn.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
    conn.commit()
    conn.close()


def test_a_past_experiment_shows_as_ended_and_asks_for_a_decision(auth_client, cleanup):
    start = date.today() - timedelta(weeks=6)
    experiment_id = _experiment(start, 2)
    cleanup.append(experiment_id)
    _, end = experiment_calendar(start, 2)

    listing = auth_client.get("/experiments").text
    card = listing[listing.index(f"ZZ Ended check {start}") :]
    assert "exp-status-ended" in card[:400], "the list badge must say ended, not active"

    page = auth_client.get(f"/experiments/{experiment_id}").text
    assert "This experiment ended on" in page
    assert f'action="/experiments/{experiment_id}/complete"' in page
    assert "exp-quicklog" not in page.split("<main", 1)[1], "no quick log for a day outside the experiment"
    assert f'name="date" value="{end.isoformat()}"' in page, "the entry form starts on the last day, not today"


def test_a_running_experiment_still_logs_today(auth_client, cleanup):
    experiment_id = _experiment(date.today() - timedelta(days=2), 2)
    cleanup.append(experiment_id)

    page = auth_client.get(f"/experiments/{experiment_id}").text
    assert "This experiment ended on" not in page
    assert "exp-quicklog" in page
    assert f'name="date" value="{date.today().isoformat()}"' in page
