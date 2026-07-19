"""General experiments: metric kinds, kind-aware logging, edit lifecycle, progress math."""

import sqlite3
from datetime import date, timedelta

from conftest import csrf_token, user_db_path


def _create_general(auth_client, **overrides):
    """A 2-week behavioral experiment: count + count-with-target + scale metrics."""
    token = csrf_token(auth_client, "/experiments/new")
    data = {
        "title": "Eksperyment 14 dni",
        "description": "Gate before PMO",
        "start_date": (date.today() - timedelta(days=6)).isoformat(),
        "num_weeks": "2",
        "target_min": "180",  # must be zeroed server-side: no duration metric
        "target_max": "210",
        "metric_names": ["Impuls", "Bramka", "Glod przed"],
        "metric_colors": ["#ef4444", "#22c55e", "#eab308"],
        "metric_kinds": ["count", "count", "scale"],
        "metric_targets": ["0", "8", "5"],  # scale target must be zeroed server-side
        "metric_periods": ["week", "total", "week"],
        "source_matches": ["", "", "walking"],  # must be blanked: not a duration metric
        "_csrf_token": token,
    }
    data.update(overrides)
    resp = auth_client.post("/experiments/create", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:200]
    return int(resp.headers["location"].rsplit("/", 1)[1]), token


def _metrics(exp_id):
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM experiment_activity_types WHERE experiment_id = ? ORDER BY display_order",
                (exp_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def _entries(exp_id):
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM experiment_entries WHERE experiment_id = ? ORDER BY id", (exp_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _cleanup(exp_id):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
        conn.commit()
    finally:
        conn.close()


def test_create_general_experiment_kinds_and_targets(auth_client):
    exp_id, _ = _create_general(auth_client)
    try:
        metrics = _metrics(exp_id)
        assert [m["kind"] for m in metrics] == ["count", "count", "scale"]
        bramka = metrics[1]
        assert bramka["target_value"] == 8
        assert bramka["target_period"] == "total"
        # Targets/source_match only exist where they mean something.
        assert metrics[2]["target_value"] == 0, "scale metrics must not carry targets"
        assert metrics[2]["source_match"] == "", "source_match is duration-only"

        # No duration metric → weekly minute targets zeroed.
        conn = sqlite3.connect(user_db_path())
        try:
            weeks = conn.execute(
                "SELECT target_min, target_max FROM experiment_weeks WHERE experiment_id = ?", (exp_id,)
            ).fetchall()
        finally:
            conn.close()
        assert len(weeks) == 2
        assert all(w == (0, 0) for w in weeks)
    finally:
        _cleanup(exp_id)


def test_boolean_upsert_one_per_day(auth_client):
    exp_id, token = _create_general(
        auth_client,
        metric_names=["Medytacja"],
        metric_colors=["#22c55e"],
        metric_kinds=["boolean"],
        metric_targets=["1"],
        metric_periods=["day"],
        source_matches=[""],
    )
    try:
        mid = _metrics(exp_id)[0]["id"]
        day = date.today().isoformat()
        for value in ("1", "1", "0"):
            resp = auth_client.post(
                f"/experiments/{exp_id}/entry",
                data={"date": day, "metric_id": str(mid), "value": value, "_csrf_token": token},
                follow_redirects=False,
            )
            assert resp.status_code == 303
        rows = _entries(exp_id)
        assert len(rows) == 1, "boolean must upsert to one row per metric per day"
        assert rows[0]["value"] == 0, "last write wins"
    finally:
        _cleanup(exp_id)


def test_count_accumulates_and_scale_bounds(auth_client):
    exp_id, token = _create_general(auth_client)
    try:
        metrics = _metrics(exp_id)
        impuls, glod = metrics[0]["id"], metrics[2]["id"]
        day = date.today().isoformat()
        for _ in range(3):
            auth_client.post(
                f"/experiments/{exp_id}/entry",
                data={"date": day, "metric_id": str(impuls), "value": "1", "_csrf_token": token},
                follow_redirects=False,
            )
        assert sum(e["value"] for e in _entries(exp_id) if e["activity_type_id"] == impuls) == 3

        # Scale 11 is out of bounds → rejected, no row.
        auth_client.post(
            f"/experiments/{exp_id}/entry",
            data={"date": day, "metric_id": str(glod), "value": "11", "_csrf_token": token},
            follow_redirects=False,
        )
        assert not [e for e in _entries(exp_id) if e["activity_type_id"] == glod]
    finally:
        _cleanup(exp_id)


def test_edit_works_on_completed_experiment(auth_client):
    exp_id, token = _create_general(auth_client)
    try:
        auth_client.post(
            f"/experiments/{exp_id}/complete",
            data={"new_status": "completed", "_csrf_token": token},
            follow_redirects=False,
        )
        assert auth_client.get(f"/experiments/{exp_id}/edit").status_code == 200

        resp = auth_client.post(
            f"/experiments/{exp_id}/edit",
            data={
                "title": "Renamed after completion",
                "description": "",
                "start_date": "2026-07-06",
                "num_weeks": "4",
                "status": "completed",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        conn = sqlite3.connect(user_db_path())
        try:
            row = conn.execute("SELECT title, status, num_weeks FROM experiments WHERE id = ?", (exp_id,)).fetchone()
            weeks = conn.execute("SELECT COUNT(*) FROM experiment_weeks WHERE experiment_id = ?", (exp_id,)).fetchone()[
                0
            ]
        finally:
            conn.close()
        assert row == ("Renamed after completion", "completed", 4)
        assert weeks == 4, "growing num_weeks must add week rows"
    finally:
        _cleanup(exp_id)


def test_num_weeks_shrink_preserves_kept_weeks(auth_client):
    exp_id, token = _create_general(auth_client, num_weeks="4")
    try:
        # Customize week 2, then shrink to 2 weeks.
        auth_client.post(
            f"/experiments/{exp_id}/week/2/targets",
            data={"target_min": "100", "target_max": "120", "label": "DELOAD", "_csrf_token": token},
            follow_redirects=False,
        )
        auth_client.post(
            f"/experiments/{exp_id}/edit",
            data={
                "title": "Eksperyment 14 dni",
                "description": "",
                "start_date": "2026-07-06",
                "num_weeks": "2",
                "status": "active",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        conn = sqlite3.connect(user_db_path())
        try:
            weeks = conn.execute(
                "SELECT week_number, target_min, target_max, label FROM experiment_weeks "
                "WHERE experiment_id = ? ORDER BY week_number",
                (exp_id,),
            ).fetchall()
        finally:
            conn.close()
        assert [w[0] for w in weeks] == [1, 2]
        assert weeks[1] == (2, 100, 120, "DELOAD"), "kept weeks must preserve their edits"
    finally:
        _cleanup(exp_id)


def test_metric_add_update_delete(auth_client):
    exp_id, token = _create_general(auth_client)
    try:
        # Add a boolean metric via the edit page.
        auth_client.post(
            f"/experiments/{exp_id}/metric/add",
            data={
                "name": "Medytacja",
                "kind": "boolean",
                "color": "#3b82f6",
                "target_value": "1",
                "target_period": "day",
                "source_match": "",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        metrics = _metrics(exp_id)
        assert len(metrics) == 4
        med = next(m for m in metrics if m["name"] == "Medytacja")
        assert (med["kind"], med["target_value"], med["target_period"]) == ("boolean", 1, "day")

        # Update: rename + retarget; kind must NOT change even if posted.
        auth_client.post(
            f"/experiments/{exp_id}/metric/{med['id']}/update",
            data={
                "name": "Medytacja AM",
                "color": "#3b82f6",
                "target_value": "5",
                "target_period": "week",
                "source_match": "",
                "kind": "duration",  # hostile input — route must ignore it
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        med2 = next(m for m in _metrics(exp_id) if m["id"] == med["id"])
        assert med2["name"] == "Medytacja AM"
        assert med2["kind"] == "boolean", "kind is immutable after creation"
        assert (med2["target_value"], med2["target_period"]) == (5, "week")

        # Delete cascades entries.
        day = date.today().isoformat()
        auth_client.post(
            f"/experiments/{exp_id}/entry",
            data={"date": day, "metric_id": str(med["id"]), "value": "1", "_csrf_token": token},
            follow_redirects=False,
        )
        auth_client.post(
            f"/experiments/{exp_id}/metric/{med['id']}/delete",
            data={"_csrf_token": token},
            follow_redirects=False,
        )
        assert not [m for m in _metrics(exp_id) if m["id"] == med["id"]]
        assert not [e for e in _entries(exp_id) if e["activity_type_id"] == med["id"]]
    finally:
        _cleanup(exp_id)


def test_metric_progress_math():
    from datetime import date

    from app.routers.experiments import _metric_progress

    start = date(2026, 7, 6)  # a Monday
    today = date(2026, 7, 10)  # Friday of week 1 → 5 elapsed days
    metric = {"id": 1, "name": "Bramka", "color": "#fff", "kind": "count", "target_value": 8, "target_period": "total"}
    entries = [
        {"activity_type_id": 1, "date": "2026-07-07", "value": 3},
        {"activity_type_id": 1, "date": "2026-07-09", "value": 2},
        {"activity_type_id": 2, "date": "2026-07-09", "value": 99},  # other metric — ignored
    ]
    p = _metric_progress(metric, entries, start, 2, today)
    assert p["label"] == "5/8"
    assert p["pct"] == 62
    assert p["met"] is False

    daily = {
        "id": 3,
        "name": "Medytacja",
        "color": "#fff",
        "kind": "boolean",
        "target_value": 1,
        "target_period": "day",
    }
    entries = [
        {"activity_type_id": 3, "date": "2026-07-06", "value": 1},
        {"activity_type_id": 3, "date": "2026-07-07", "value": 1},
        {"activity_type_id": 3, "date": "2026-07-08", "value": 0},
        {"activity_type_id": 3, "date": "2026-07-09", "value": 1},
    ]
    p = _metric_progress(daily, entries, start, 2, today)
    assert p["label"] == "3/5 days"
    assert p["met"] is False

    # No target → no progress chip.
    none_metric = {
        "id": 4,
        "name": "Impuls",
        "color": "#fff",
        "kind": "count",
        "target_value": 0,
        "target_period": "week",
    }
    assert _metric_progress(none_metric, [], start, 2, today) is None
