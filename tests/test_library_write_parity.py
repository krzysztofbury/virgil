"""I1 (2026-07-30 review): app/routers/settings.py (form) and app/routers/api.py
(REST) write the same exercise_library table but used to reach DIFFERENT
accept/reject decisions for identical bad input — an invalid section/metric
was silently coerced through settings.py and 422'd through api.py; a
duplicate name or rename collision silently no-op'd through settings.py and
409'd through api.py; a builtin edit/delete silently no-op'd through
settings.py and 409'd through api.py. Both surfaces now route through
app/library_validation.py's validate_library_write, so this file feeds the
identical input to both and asserts they agree.

I2 (same review): a rename must be refused, on both surfaces, when a
training_exercises row still holds history under the old name — otherwise the
next WOD mentioning the new name creates a second row and splits the
movement's PBs/volume in two.
"""

import sqlite3
from urllib.parse import unquote

from conftest import csrf_token, user_db_path

KEY = {"X-API-Key": "test-key-123"}


def _row(name: str) -> dict | None:
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM exercise_library WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _delete_row(name: str) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM exercise_library WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


def _insert_training_exercise(name: str) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("INSERT INTO training_exercises (name, section) VALUES (?, 'Core')", (name,))
        conn.commit()
    finally:
        conn.close()


def _delete_training_exercise(name: str) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM training_exercises WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


def _any_builtin_non_crossfit() -> dict:
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(
            "SELECT * FROM exercise_library WHERE builtin = 1 AND category != 'CrossFit' LIMIT 1"
        ).fetchone()
        return dict(r)
    finally:
        conn.close()


def test_invalid_section_rejected_by_both_surfaces(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Parity Bad Section",
            "category": "Parity Test",
            "section": "NotASection",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "reps",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"], "settings must reject an invalid section, not coerce to Core"
    assert _row("Parity Bad Section") is None

    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"category": "Parity Test", "section": "NotASection", "name": "Parity Bad Section", "metric": "reps"},
    )
    assert resp.status_code == 422
    assert _row("Parity Bad Section") is None


def test_invalid_metric_rejected_by_both_surfaces(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Parity Bad Metric",
            "category": "Parity Test",
            "section": "Core",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "bogus",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    location = resp.headers["location"]
    assert "err=" in location, "settings must reject an invalid metric, not coerce to reps"
    # I1's design is that BOTH surfaces reach validate_library_write and carry
    # its message through — not just that both happen to reject. Asserting
    # only "err=" / only the status code (as this test used to) is satisfied
    # by any rejection reason at all, including an unrelated one substituted
    # in by mistake; pin the actual, stable reason text on both surfaces.
    assert "metric must be one of" in unquote(location), (
        "settings must surface the SAME rejection reason validate_library_write raised, not just any err="
    )
    assert _row("Parity Bad Metric") is None

    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"category": "Parity Test", "section": "Core", "name": "Parity Bad Metric", "metric": "bogus"},
    )
    assert resp.status_code == 422
    assert "metric must be one of" in resp.json()["detail"], (
        "the API must surface the SAME rejection reason as settings, not just the same status code"
    )
    assert _row("Parity Bad Metric") is None


def test_duplicate_name_rejected_by_both_surfaces(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    try:
        resp = auth_client.post(
            "/settings/library/add",
            data={
                "name": "Parity Dup",
                "category": "Parity Test",
                "section": "Core",
                "sets": "",
                "reps": "",
                "notes": "",
                "metric": "reps",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert "err=" not in resp.headers["location"]
        assert _row("Parity Dup") is not None

        # settings.py used to INSERT OR IGNORE here — a silent success with no
        # `err`, indistinguishable from the first, real add.
        resp = auth_client.post(
            "/settings/library/add",
            data={
                "name": "Parity Dup",
                "category": "Parity Test",
                "section": "Core",
                "sets": "",
                "reps": "",
                "notes": "",
                "metric": "reps",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert "err=" in resp.headers["location"], "a duplicate (category, name) must be refused loudly"

        resp = auth_client.post(
            "/api/library",
            headers=KEY,
            json={"category": "Parity Test", "section": "Core", "name": "Parity Dup", "metric": "reps"},
        )
        assert resp.status_code == 409
    finally:
        _delete_row("Parity Dup")


def test_builtin_edit_rejected_by_both_surfaces(auth_client):
    builtin = _any_builtin_non_crossfit()
    token = csrf_token(auth_client, "/settings?tab=configuration")

    resp = auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(builtin["id"]),
            "name": builtin["name"],
            "section": builtin["section"],
            "sets": "",
            "reps": "",
            "notes": "PARITY HACK",
            "metric": builtin["metric"],
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"], "settings must refuse a builtin edit loudly, not silently no-op"
    assert _row(builtin["name"])["notes"] != "PARITY HACK"

    resp = auth_client.patch(f"/api/library/{builtin['id']}", headers=KEY, json={"notes": "PARITY HACK"})
    assert resp.status_code == 409
    assert _row(builtin["name"])["notes"] != "PARITY HACK"


def test_builtin_delete_rejected_by_both_surfaces(auth_client):
    builtin = _any_builtin_non_crossfit()
    token = csrf_token(auth_client, "/settings?tab=configuration")

    resp = auth_client.post(
        "/settings/library/delete",
        data={"entry_id": str(builtin["id"]), "_csrf_token": token},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"], "settings must refuse a builtin delete loudly, not silently no-op"
    assert _row(builtin["name"]) is not None

    resp = auth_client.delete(f"/api/library/{builtin['id']}", headers=KEY)
    assert resp.status_code == 409
    assert _row(builtin["name"]) is not None


def test_update_omitting_metric_leaves_it_unchanged_not_reset(auth_client):
    """I1 regression: settings.py's library_update used to declare
    `metric: str = Form("reps")` — a POST that omitted `metric` entirely
    (e.g. a settings page cached in a browser from before this branch added
    the metric <select>) silently rewrote the column back to 'reps'. This
    simulates that exact stale-form POST: no `metric` key in the body at all,
    not merely a blank one."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Parity Stale Form",
            "category": "Parity Test",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "time",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert "err=" not in resp.headers["location"]
    entry_id = _row("Parity Stale Form")["id"]
    try:
        # No "metric" key at all in this POST body.
        auth_client.post(
            "/settings/library/update",
            data={
                "entry_id": str(entry_id),
                "name": "Parity Stale Form",
                "section": "Cardio",
                "sets": "",
                "reps": "",
                "notes": "edited without touching metric",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        row = _row("Parity Stale Form")
        assert row["metric"] == "time", "an absent `metric` field must leave the column unchanged, not reset it"
        assert row["notes"] == "edited without touching metric", "other, present fields must still save"
    finally:
        _delete_row("Parity Stale Form")


def test_rename_refused_when_training_history_exists_settings(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={
            "name": "Parity Muscle-up",
            "category": "Parity Test",
            "section": "Core",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "reps",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    entry_id = _row("Parity Muscle-up")["id"]
    _insert_training_exercise("Parity Muscle-up")

    try:
        resp = auth_client.post(
            "/settings/library/update",
            data={
                "entry_id": str(entry_id),
                "name": "Parity Bar Muscle-up",
                "section": "Core",
                "sets": "",
                "reps": "",
                "notes": "",
                "metric": "reps",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert "err=" in resp.headers["location"], "a rename must be refused while training history exists"
        assert _row("Parity Muscle-up") is not None, "the old library name must survive the refused rename"
        assert _row("Parity Bar Muscle-up") is None
    finally:
        _delete_row("Parity Muscle-up")
        _delete_training_exercise("Parity Muscle-up")


def test_rename_refused_when_training_history_exists_api(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"category": "Parity Test", "section": "Core", "name": "Parity Snatch", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    _insert_training_exercise("Parity Snatch")

    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"name": "Parity Power Snatch"})
        assert resp.status_code == 409, "a rename must be refused while training history exists"
        assert _row("Parity Snatch") is not None
        assert _row("Parity Power Snatch") is None
    finally:
        _delete_row("Parity Snatch")
        _delete_training_exercise("Parity Snatch")


def test_rename_allowed_when_no_training_history_exists(auth_client):
    """Control: the I2 guard must only fire when training_exercises actually
    holds a matching row — a plain rename with no history must still work on
    both surfaces."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={
            "name": "Parity No History",
            "category": "Parity Test",
            "section": "Core",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "reps",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    entry_id = _row("Parity No History")["id"]
    try:
        resp = auth_client.post(
            "/settings/library/update",
            data={
                "entry_id": str(entry_id),
                "name": "Parity Renamed Fine",
                "section": "Core",
                "sets": "",
                "reps": "",
                "notes": "",
                "metric": "reps",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert "err=" not in resp.headers["location"]
        assert _row("Parity No History") is None
        assert _row("Parity Renamed Fine") is not None
    finally:
        _delete_row("Parity No History")
        _delete_row("Parity Renamed Fine")
