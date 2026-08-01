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


def test_add_rejects_invalid_metric(auth_client):
    """I1 (2026-07-30 review): this used to assert the OPPOSITE — that an
    invalid metric silently coerced to 'reps' and the row was created anyway.
    That was ratifying a defect: app/routers/api.py rejects the identical
    input with a 422, so the same bad value was garbage-in-silently-fixed on
    one write surface and loudly refused on the other. Both surfaces now
    share one policy (validate_library_write in app/library_validation.py):
    an invalid metric must be refused, not coerced, on both."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/add",
        data={
            "name": "Bogus Metric Move",
            "section": "Cardio",
            "sets": "",
            "reps": "",
            "notes": "",
            "metric": "not-a-real-metric",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"], "an invalid metric must be rejected loudly, not silently coerced"
    assert _row("Bogus Metric Move") is None, "no row must be created for a rejected metric"


def test_add_with_tags_normalises_them(auth_client):
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={
            "name": "Sandbag Carry",
            "section": "Core",
            "metric": "time",
            "tags": "Strongman, KETTLEBELL ",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    conn = sqlite3.connect(user_db_path())
    try:
        row = conn.execute("SELECT id FROM exercise_library WHERE name = 'Sandbag Carry'").fetchone()
        assert row, "row must be created"
        tags = [
            r[0]
            for r in conn.execute("SELECT tag FROM exercise_library_tags WHERE library_id = ? ORDER BY tag", (row[0],))
        ]
    finally:
        conn.close()
    assert tags == ["kettlebell", "strongman"]


def test_update_can_tag_a_builtin_row(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        row = conn.execute("SELECT id FROM exercise_library WHERE builtin = 1 LIMIT 1").fetchone()
    finally:
        conn.close()
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/update",
        data={"entry_id": str(row[0]), "tags": "mobility", "_csrf_token": token},
        follow_redirects=False,
    )
    conn = sqlite3.connect(user_db_path())
    try:
        tags = [r[0] for r in conn.execute("SELECT tag FROM exercise_library_tags WHERE library_id = ?", (row[0],))]
    finally:
        conn.close()
    assert "mobility" in tags, "builtin must not block tag writes"


def test_update_rejects_combined_name_and_tags_change_on_builtin_row(auth_client):
    """Tags are not gated by builtin on their own, but a request that ALSO
    touches a frozen field (name here) on the same builtin row must be
    rejected wholesale — neither half may land. `fields` (which still
    contains `name`) reaches validate_library_write's builtin guard exactly
    as it does for a name-only update; that guard raises and the function
    returns before the tags branch ever runs, so a request mixing a frozen
    field with `tags` is refused in full, not partially applied."""
    builtin = _any_builtin_id()
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/update",
        data={
            "entry_id": str(builtin["id"]),
            "name": "Hacked Builtin Combo",
            "tags": "sneaky-combo",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    conn = sqlite3.connect(user_db_path())
    try:
        current_name = conn.execute("SELECT name FROM exercise_library WHERE id = ?", (builtin["id"],)).fetchone()[0]
        tags = [
            r[0]
            for r in conn.execute(
                "SELECT tag FROM exercise_library_tags WHERE library_id = ? ORDER BY tag", (builtin["id"],)
            )
        ]
    finally:
        conn.close()
    assert current_name == builtin["name"], "a rejected combined update must not rename the builtin row"
    assert "sneaky-combo" not in tags, "a rejected combined update must not apply the tags half either"


def test_update_without_tags_field_leaves_existing_tags_alone(auth_client):
    """A settings-form update that never mentions `tags` (e.g. editing notes
    only) must not wipe the row's existing tag set — same "omitted means
    untouched" contract the REST PATCH endpoint enforces (Task 3 caught this
    exact defect class: a stray default of `Form("")` instead of `Form(None)`
    would make every such save silently clear tags, and every OTHER test
    would still pass since none of them re-check tag survival)."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={"name": "Farmers Walk", "section": "Core", "tags": "grip", "_csrf_token": token},
        follow_redirects=False,
    )
    row = _row("Farmers Walk")

    auth_client.post(
        "/settings/library/update",
        data={"entry_id": str(row["id"]), "name": "Farmers Walk", "notes": "heavy", "_csrf_token": token},
        follow_redirects=False,
    )
    conn = sqlite3.connect(user_db_path())
    try:
        tags = [r[0] for r in conn.execute("SELECT tag FROM exercise_library_tags WHERE library_id = ?", (row["id"],))]
    finally:
        conn.close()
    assert tags == ["grip"], "an update that never mentions tags must not wipe them"


def test_update_with_blank_tags_clears_them(auth_client):
    """The flip side of the test above: a `tags` field that IS present but
    blank (the user cleared the text input and hit Save) must clear the tag
    set, exactly like the REST PATCH's `tags: []` — "present but empty" is
    not the same as "absent"."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    auth_client.post(
        "/settings/library/add",
        data={"name": "Sled Drag", "section": "Core", "tags": "strongman", "_csrf_token": token},
        follow_redirects=False,
    )
    row = _row("Sled Drag")

    auth_client.post(
        "/settings/library/update",
        data={"entry_id": str(row["id"]), "tags": "", "_csrf_token": token},
        follow_redirects=False,
    )
    conn = sqlite3.connect(user_db_path())
    try:
        tags = [r[0] for r in conn.execute("SELECT tag FROM exercise_library_tags WHERE library_id = ?", (row["id"],))]
    finally:
        conn.close()
    assert tags == [], "an explicit blank tags field must clear the tag set"


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


def test_archive_missing_entry_redirects_with_error(auth_client):
    """M1 (2026-07-31 review): /settings/library/archive had no existence
    check and never called validate_library_write at all -- a bogus id
    silently updated zero rows and redirected as if it had succeeded, while
    api.py's PATCH 404s for the same input. That was the one library write
    where the two surfaces disagreed on "does this row exist", contradicting
    library_validation.py's own claim of being the one shared decision point."""
    token = csrf_token(auth_client, "/settings?tab=configuration")
    resp = auth_client.post(
        "/settings/library/archive",
        data={"entry_id": "999999999", "archived": "1", "_csrf_token": token},
        follow_redirects=False,
    )
    assert "err=" in resp.headers["location"], (
        "archiving a nonexistent entry must redirect with an error, not silently no-op"
    )
