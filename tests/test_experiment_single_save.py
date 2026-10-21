"""The edit page saves its basics and every metric with one button (#95).

A Save per metric row next to "Save Changes" lost what was typed in the rows
that were not saved. One bad row must leave the whole page unsaved.
"""

import sqlite3

import pytest

from tests.conftest import csrf_token, user_db_path


@pytest.fixture
def experiment():
    conn = sqlite3.connect(user_db_path())
    experiment_id = conn.execute(
        "INSERT INTO experiments (title, description, start_date, num_weeks, status) "
        "VALUES ('ZZ Single save', '', '2026-10-01', 2, 'active')"
    ).lastrowid
    metrics = [
        conn.execute(
            "INSERT INTO experiment_activity_types (experiment_id, name, color, kind, target_value, target_period, "
            "display_order) VALUES (?, ?, '#22c55e', 'count', 1, 'week', ?)",
            (experiment_id, name, order),
        ).lastrowid
        for order, name in enumerate(("ZZ Walk", "ZZ Read"))
    ]
    conn.commit()
    conn.close()
    yield experiment_id, metrics
    conn = sqlite3.connect(user_db_path())
    conn.execute("DELETE FROM experiment_weeks WHERE experiment_id = ?", (experiment_id,))
    conn.execute("DELETE FROM experiment_activity_types WHERE experiment_id = ?", (experiment_id,))
    conn.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
    conn.commit()
    conn.close()


def _post(client, experiment_id, **metric_fields):
    token = csrf_token(client, f"/experiments/{experiment_id}/edit")
    data = {
        "_csrf_token": token,
        "title": "ZZ Single save, renamed",
        "description": "",
        "start_date": "2026-10-01",
        "num_weeks": "3",
        "status": "active",
        **metric_fields,
    }
    return client.post(f"/experiments/{experiment_id}/edit", data=data, follow_redirects=False)


def _state(experiment_id):
    conn = sqlite3.connect(user_db_path())
    try:
        title, weeks = conn.execute(
            "SELECT title, num_weeks FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        metrics = conn.execute(
            "SELECT name, target_value, target_period FROM experiment_activity_types WHERE experiment_id = ? "
            "ORDER BY display_order",
            (experiment_id,),
        ).fetchall()
        return title, weeks, metrics
    finally:
        conn.close()


def test_one_save_writes_the_basics_and_every_metric(auth_client, experiment):
    experiment_id, (walk, read) = experiment
    response = _post(
        auth_client,
        experiment_id,
        **{
            f"metric_{walk}_name": "ZZ Walk outside",
            f"metric_{walk}_color": "#22c55e",
            f"metric_{walk}_target_value": "5",
            f"metric_{walk}_target_period": "week",
            f"metric_{read}_name": "ZZ Read",
            f"metric_{read}_color": "#22c55e",
            f"metric_{read}_target_value": "1",
            f"metric_{read}_target_period": "day",
        },
    )
    assert response.status_code == 303
    title, weeks, metrics = _state(experiment_id)
    assert (title, weeks) == ("ZZ Single save, renamed", 3)
    assert metrics == [("ZZ Walk outside", 5, "week"), ("ZZ Read", 1, "day")]


def test_one_bad_row_saves_nothing(auth_client, experiment):
    experiment_id, (walk, read) = experiment
    response = _post(
        auth_client,
        experiment_id,
        **{f"metric_{walk}_name": "zz read", f"metric_{read}_name": "ZZ Read"},
    )
    assert response.status_code == 303
    assert "unique" in response.headers["location"]
    title, weeks, metrics = _state(experiment_id)
    assert (title, weeks) == ("ZZ Single save", 2), "the basics must not be saved either"
    assert [name for name, _, _ in metrics] == ["ZZ Walk", "ZZ Read"]
