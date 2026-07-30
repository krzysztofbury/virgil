"""App Configuration dictionary: exercise library CRUD with builtin protection."""

import sqlite3

from conftest import csrf_token, user_db_path


def _row(name):
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM exercise_library WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _any_builtin_id():
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM exercise_library WHERE builtin = 1 LIMIT 1").fetchone())
    finally:
        conn.close()


def test_configuration_tab_renders(auth_client):
    resp = auth_client.get("/settings?tab=configuration")
    assert resp.status_code == 200
    assert "Exercise Library" in resp.text
    assert "built-in" in resp.text


def test_add_edit_delete_user_row(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Nordic Curl",
            "category": "Home gym",
            "section": "Core",
            "sets": "3",
            "reps": "5-8",
            "notes": "slow eccentric",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = _row("Nordic Curl")
    assert row is not None
    assert row["builtin"] == 0
    assert row["archived"] == 0

    # Edit own row.
    auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(row["id"]),
            "name": "Nordic Curl (band)",
            "section": "Core",
            "sets": "4",
            "reps": "5",
            "notes": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert _row("Nordic Curl") is None
    edited = _row("Nordic Curl (band)")
    assert edited["sets"] == 4

    # Delete own row.
    auth_client.post(
        "/settings/library/delete",
        data={"entry_id": str(edited["id"]), "_csrf_token": token},
        follow_redirects=False,
    )
    assert _row("Nordic Curl (band)") is None


def test_builtin_rows_protected(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    builtin = _any_builtin_id()

    # Update must be a no-op.
    auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(builtin["id"]),
            "name": "HACKED",
            "section": "Core",
            "sets": "1",
            "reps": "",
            "notes": "",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert _row("HACKED") is None
    assert _row(builtin["name"]) is not None

    # Delete must be a no-op.
    auth_client.post(
        "/settings/library/delete",
        data={"entry_id": str(builtin["id"]), "_csrf_token": token},
        follow_redirects=False,
    )
    assert _row(builtin["name"]) is not None


def test_add_with_metric_time_is_persisted(auth_client):
    """A user adding a cardio/erg movement through Settings -> App Config with
    metric=time must not silently fall back to the reps column default: a
    'time' movement logged in minutes/seconds must never land in the weekly
    Total Reps aggregate (app/routers/training.py sums metric='reps' rows only).
    """
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Echo Bike",
            "category": "Cardio",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "time",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = _row("Echo Bike")
    assert row is not None
    assert row["metric"] == "time", "settings form must persist metric, not silently default to 'reps'"


def test_update_changes_metric(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={
            "name": "Ski Erg Intervals",
            "category": "Cardio",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "reps",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    row = _row("Ski Erg Intervals")
    assert row["metric"] == "reps"

    auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(row["id"]),
            "name": "Ski Erg Intervals",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "time",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert _row("Ski Erg Intervals")["metric"] == "time", "update must persist a changed metric, not ignore it"


def test_add_rejects_invalid_metric_defaults_to_reps(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={
            "name": "Bogus Metric Move",
            "category": "Cardio",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "not-a-real-metric",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    row = _row("Bogus Metric Move")
    assert row is not None
    assert row["metric"] == "reps", "an invalid metric must fall back to the safe default, not persist garbage"


def test_archive_hides_from_training_picker(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    builtin = _any_builtin_id()
    # The picker embeds library rows as option JSON ({"n": <name>, ...}); the bare
    # name can also appear on the page via the user's protocol, so match the JSON.
    picker_marker = f'{{"n": "{builtin["name"]}"'

    auth_client.post(
        "/settings/library/archive",
        data={"entry_id": str(builtin["id"]), "archived": "1", "_csrf_token": token},
        follow_redirects=False,
    )
    assert _row(builtin["name"])["archived"] == 1
    assert picker_marker not in auth_client.get("/training").text, "archived entries must leave the picker"

    # Restore.
    auth_client.post(
        "/settings/library/archive",
        data={"entry_id": str(builtin["id"]), "archived": "0", "_csrf_token": token},
        follow_redirects=False,
    )
    assert _row(builtin["name"])["archived"] == 0
    assert picker_marker in auth_client.get("/training").text
