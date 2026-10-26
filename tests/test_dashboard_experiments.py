"""Active experiments are logged from their dashboard card, not only from their page."""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, user_db_path


def _experiment() -> tuple[int, int, int]:
    """An active experiment with a yes/no and a count metric, newest on the dashboard."""
    start = (date.today() - timedelta(days=2)).isoformat()
    with sqlite3.connect(user_db_path()) as conn:
        exp_id = conn.execute(
            "INSERT INTO experiments (title, description, start_date, num_weeks, status, created_at) "
            "VALUES ('ZZ Card Probe', '', ?, 2, 'active', '2999-01-01 00:00:00')",
            (start,),
        ).lastrowid
        done = conn.execute(
            "INSERT INTO experiment_activity_types (experiment_id, name, kind, color) VALUES (?, 'Meditate', 'boolean', '#22c55e')",
            (exp_id,),
        ).lastrowid
        urges = conn.execute(
            "INSERT INTO experiment_activity_types (experiment_id, name, kind, color) VALUES (?, 'Urge', 'count', '#ef4444')",
            (exp_id,),
        ).lastrowid
    return exp_id, done, urges


def _remove(exp_id: int) -> None:
    with sqlite3.connect(user_db_path()) as conn:
        for table in ("experiment_entries", "experiment_activity_types"):
            conn.execute(f"DELETE FROM {table} WHERE experiment_id = ?", (exp_id,))  # noqa: S608 - fixed names
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))


def test_a_card_logs_today_in_place_and_shows_what_is_logged(auth_client):
    exp_id, done, urges = _experiment()
    today = date.today().isoformat()
    try:
        page = auth_client.get("/").text
        card = page[page.index(f'id="exp-card-{exp_id}"') :]
        assert f'hx-post="/experiments/{exp_id}/entry"' in card

        token = csrf_token(auth_client, "/")
        tapped = auth_client.post(
            f"/experiments/{exp_id}/entry",
            data={"date": today, "metric_id": done, "value": "1", "view": "dashboard", "_csrf_token": token},
            headers={"HX-Request": "true"},
        )
        assert tapped.status_code == 200
        assert tapped.text.lstrip().startswith(f'<div class="card exp-card-dash" id="exp-card-{exp_id}"')
        assert 'aria-pressed="true"' in tapped.text, "today's answer shows on the redrawn card"

        # Without htmx the form posts and lands back on the dashboard.
        plain = auth_client.post(
            f"/experiments/{exp_id}/entry",
            data={"date": today, "metric_id": urges, "value": "1", "view": "dashboard", "_csrf_token": token},
            follow_redirects=False,
        )
        assert plain.status_code == 303 and plain.headers["location"].split("?")[0] == "/"
        assert "1 today" in auth_client.get("/").text

        with sqlite3.connect(user_db_path()) as conn:
            rows = conn.execute(
                "SELECT activity_type_id, value FROM experiment_entries WHERE experiment_id = ? ORDER BY id", (exp_id,)
            ).fetchall()
        assert rows == [(done, 1), (urges, 1)]
    finally:
        _remove(exp_id)


def test_an_experiment_past_its_last_day_leaves_the_dashboard(auth_client):
    """It stays "active" until marked completed, but the Experiments page already
    shows it as ended; the dashboard must not offer to log it for today (#163)."""
    exp_id, _done, _urges = _experiment()
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute("UPDATE experiments SET start_date = ? WHERE id = ?", ("2020-01-06", exp_id))
    try:
        assert f'id="exp-card-{exp_id}"' not in auth_client.get("/").text
    finally:
        _remove(exp_id)
