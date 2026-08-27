"""Experiments: create-time target normalization + reopen after complete/abandon."""

import sqlite3

from conftest import csrf_token, user_db_path


def test_inverted_targets_normalized_and_reopen_allowed(auth_client):
    token = csrf_token(auth_client, "/experiments/new")
    resp = auth_client.post(
        "/experiments/create",
        data={
            "title": "Test Experiment",
            "start_date": "2026-07-06",
            "num_weeks": "4",
            "target_min": "200",
            "target_max": "100",  # inverted on purpose
            "metric_names": "Zone 2",
            "metric_colors": "#22c55e",
            "metric_kinds": "duration",
            "metric_targets": "0",
            "metric_periods": "week",
            "source_matches": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    exp_id = int(resp.headers["location"].rsplit("/", 1)[1])

    conn = sqlite3.connect(user_db_path())
    try:
        weeks = conn.execute(
            "SELECT target_min, target_max FROM experiment_weeks WHERE experiment_id = ?", (exp_id,)
        ).fetchall()
        assert len(weeks) == 4
        for target_min, target_max in weeks:
            assert target_min == 200
            assert target_max == 200, "target_max must be raised to target_min, not stay inverted"
    finally:
        conn.close()

    # Complete, then reopen (undo).
    for status in ("completed", "active"):
        resp = auth_client.post(
            f"/experiments/{exp_id}/complete",
            data={"new_status": status, "_csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        conn = sqlite3.connect(user_db_path())
        try:
            stored = conn.execute("SELECT status FROM experiments WHERE id = ?", (exp_id,)).fetchone()[0]
            assert stored == status
        finally:
            conn.close()

    # Bogus status is ignored.
    resp = auth_client.post(
        f"/experiments/{exp_id}/complete",
        data={"new_status": "exploded", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    conn = sqlite3.connect(user_db_path())
    try:
        stored = conn.execute("SELECT status FROM experiments WHERE id = ?", (exp_id,)).fetchone()[0]
        assert stored == "active"
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
        conn.commit()
    finally:
        conn.close()


def test_elapsed_percent_is_day_based(auth_client):
    """0% elapsed in week 1 of 4 is derivable but reads as a contradiction.

    progress_pct was completed WEEKS over total weeks, so the whole of week 1
    rendered "0% elapsed" beside a "Week 1/4" label.
    """
    import sqlite3
    from datetime import date, timedelta

    from conftest import user_db_path

    start = (date.today() - timedelta(days=3)).isoformat()
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "INSERT INTO experiments (title, description, start_date, num_weeks, status) "
            "VALUES ('ZZ Elapsed Probe', '', ?, 4, 'active')",
            (start,),
        )
        conn.commit()
    finally:
        conn.close()

    html = auth_client.get("/experiments").text
    block = html[html.index("ZZ Elapsed Probe") :]
    percent = block[: block.index("% elapsed")].split(">")[-1].strip()
    assert percent != "0", "four days into a 28-day experiment is not 0% elapsed"
    assert int(percent) == round(4 / 28 * 100)
