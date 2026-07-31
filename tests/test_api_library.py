"""REST CRUD over the exercise library — the dictionary MCP clients configure."""

import sqlite3

from conftest import user_db_path

KEY = {"X-API-Key": "test-key-123"}


def _get(name: str) -> dict | None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT * FROM exercise_library WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def test_library_requires_key():
    # A fresh, session-less TestClient — not the shared `client`/`auth_client`
    # fixture, which by this point in the suite already carries a valid session
    # cookie from earlier tests (they're the same session-scoped object per
    # tests/conftest.py). Against a session-authenticated client, this endpoint
    # would 401 anyway (the route's own X-API-Key check), which would pass this
    # assertion for the wrong reason and hide a PUBLIC_PATHS regression that
    # instead makes AuthMiddleware 303-redirect a genuinely anonymous caller to
    # /login (see test_new_api_routes_reachable_with_key_only in test_api.py,
    # which covers that regression class directly).
    from fastapi.testclient import TestClient

    from app.main import app

    assert TestClient(app).get("/api/library").status_code == 401


def test_list_returns_crossfit_vocabulary(auth_client):
    """`?category=` is gone (migration 019 dropped the column); GET /api/library
    now always returns the whole library. `auth_client` is the session-scoped
    fixture shared by the whole suite, so the total is a floor (>=), not an
    exact match — other test files create/delete their own rows against this
    same database and file execution order isn't guaranteed. The floor is the
    post-merge count the migration brief calls for: 46 EXERCISE_LIBRARY + 31
    CROSSFIT_MOVEMENTS - 4 merged duplicate names (Back Squat, Deadlift, Bench
    Press, Pull-up) = 73."""
    from app.exercise_library import CROSSFIT_MOVEMENTS

    body = auth_client.get("/api/library", headers=KEY).json()
    names = {e["name"] for e in body["entries"]}
    assert "Thruster" in names
    assert {m["name"] for m in CROSSFIT_MOVEMENTS} <= names, "every CrossFit movement must still be listed"
    assert len(body["entries"]) >= 73


def test_create_then_read_back(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Devil Press", "metric": "reps"},
    )
    assert resp.status_code == 201
    row = _get("Devil Press")
    assert row["section"] == "Core"
    assert row["metric"] == "reps"
    assert row["builtin"] == 0


def test_duplicate_name_is_409(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Thruster", "metric": "reps"},
    )
    assert resp.status_code == 409


def test_duplicate_name_case_insensitive_is_409(auth_client):
    """exercise_library.name is UNIQUE(name COLLATE NOCASE) (migration 019) —
    validate_library_write's own dup check must be case-insensitive too, or a
    request differing only by case would reach the DB constraint (a 500, not
    a clean 409) instead, or — if that constraint were ever weakened — would
    silently create a second row for the same movement under a different
    case, splitting the WOD parser's vocabulary."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "thruster", "metric": "reps"},
    )
    assert resp.status_code == 409


def test_create_strips_name_and_rejects_blank(auth_client):
    # A trailing space would otherwise create a second, visually-identical
    # CrossFit movement — both the picker and the WOD parser's closed
    # vocabulary would treat "Thruster " as distinct from "Thruster".
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Thruster ", "metric": "reps"},
    )
    assert resp.status_code == 409, "a stripped name must still collide with the existing 'Thruster'"

    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "   ", "metric": "reps"},
    )
    assert resp.status_code == 422


def test_create_clamps_sets(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Clamp Sets Create", "metric": "reps", "sets": 999},
    )
    assert resp.status_code == 201
    entry_id = resp.json()["id"]
    try:
        assert _get("Clamp Sets Create")["sets"] == 20
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_create_name_truncated_to_100_not_60(auth_client):
    """A name created through the settings form (100-char cap) must stay
    matchable through this API — truncating here at 60 would silently corrupt
    it. (Formerly this pinned `category`'s truncation cap; that field is gone
    since migration 019, so this now pins the same parity concern on `name`,
    the field that carries it now.)"""
    long_name = "X" * 80
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": long_name, "metric": "reps"},
    )
    assert resp.status_code == 201
    entry_id = resp.json()["id"]
    try:
        assert _get(long_name)["name"] == long_name
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_create_invalid_section_and_metric_422(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "NotASection", "name": "Bad Section Move", "metric": "reps"},
    )
    assert resp.status_code == 422
    assert _get("Bad Section Move") is None

    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Bad Metric Move", "metric": "nope"},
    )
    assert resp.status_code == 422
    assert _get("Bad Metric Move") is None


def test_create_rejects_unknown_field(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={
            "section": "Core",
            "name": "Unknown Field Move",
            "metric": "reps",
            "builtin": 1,
        },
    )
    assert resp.status_code == 422
    assert _get("Unknown Field Move") is None


def test_patch_edits_fields(auth_client):
    entry_id = _get("Devil Press")["id"]
    resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"notes": "dumbbell, brutal"})
    assert resp.status_code == 200
    assert _get("Devil Press")["notes"] == "dumbbell, brutal"


def test_patch_strips_and_rejects_blank_name(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Strip Patch Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"name": "   "})
        assert resp.status_code == 422
        assert _get("Strip Patch Target") is not None

        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"name": " Trimmed Name "})
        assert resp.status_code == 200
        assert _get("Trimmed Name")["name"] == "Trimmed Name"
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_patch_clamps_sets(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Clamp Sets Patch", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"sets": -5})
        assert resp.status_code == 200
        assert _get("Clamp Sets Patch")["sets"] == 1
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_patch_invalid_section_and_metric_422(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Patch Validation Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"section": "NotASection"})
        assert resp.status_code == 422
        assert _get("Patch Validation Target")["section"] == "Core"

        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"metric": "nope"})
        assert resp.status_code == 422
        assert _get("Patch Validation Target")["metric"] == "reps"
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_patch_rename_collision_409(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Rename Collision Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"name": "Thruster"})
        assert resp.status_code == 409
        assert _get("Rename Collision Target") is not None
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_patch_rejects_unknown_field(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Unknown Patch Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        # `category` used to appear in every GET /api/library response, so an
        # MCP client that echoed a row straight back into a PATCH body would
        # send it — migration 019 removed the column, but the field name is
        # still a plausible stale value a client might send, and it must
        # still fail loudly, not silently no-op (extra="forbid" -> 422).
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"category": "Hack"})
        assert resp.status_code == 422
        assert _get("Unknown Patch Target")["notes"] == "", "a rejected PATCH must not partially apply"
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_unknown_id_404(auth_client):
    assert auth_client.patch("/api/library/999999", headers=KEY, json={"notes": "x"}).status_code == 404
    assert auth_client.delete("/api/library/999999", headers=KEY).status_code == 404


def test_delete_removes_row(auth_client):
    entry_id = _get("Devil Press")["id"]
    assert auth_client.delete(f"/api/library/{entry_id}", headers=KEY).status_code == 204
    assert _get("Devil Press") is None


def test_builtin_row_refuses_edit_and_delete(auth_client):
    """A non-CrossFit seeded row is still builtin=1 (migration 015) — the API
    must refuse rather than silently no-op the way the settings form does."""
    row = _get("Goblet Squat")
    assert row["builtin"] == 1, "fixture assumption: legacy library rows stay protected"
    assert auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"notes": "x"}).status_code == 409
    assert auth_client.delete(f"/api/library/{row['id']}", headers=KEY).status_code == 409
    assert _get("Goblet Squat")["notes"] != "x"


def test_builtin_archived_value_is_normalized(auth_client):
    """archived is the one field the builtin guard lets through — left
    uncoerced it's the one way to park an out-of-domain value on a protected
    row: invisible to `archived = 0` filters, never matched by `archived = 1`."""
    row = _get("Goblet Squat")
    resp = auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 99})
    assert resp.status_code == 200
    assert _get("Goblet Squat")["archived"] == 1
    auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 0})


def test_builtin_row_can_still_be_archived(auth_client):
    row = _get("Goblet Squat")
    assert auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 1}).status_code == 200
    assert _get("Goblet Squat")["archived"] == 1
    auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 0})


def test_wod_entries_surface_in_training_reads(auth_client):
    """Entries written against an ad_hoc movement must appear in the machine-facing
    reads, not just the UI — this is what an MCP client sees."""
    conn = sqlite3.connect(user_db_path())
    sess_id = ex_id = None
    try:
        cur = conn.execute(
            "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('Ski Erg', 'Cardio', 'time', 1)"
        )
        ex_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes) "
            "VALUES (date('now'), 40, 'ski erg intervals')"
        )
        sess_id = cur.lastrowid
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
            "VALUES (?, ?, 1, NULL, NULL, 300)",
            (sess_id, ex_id),
        )
        conn.commit()

        detail = auth_client.get("/api/training/detail", headers=KEY, params={"range": 7}).json()
        blob = str(detail)
        assert "Ski Erg" in blob, "ad_hoc movements must be visible to API/MCP consumers"
        assert "ski erg intervals" in blob, "the raw WOD note must reach API consumers"
    finally:
        # Leave the shared session-scoped DB empty for other test files —
        # tests/test_api.py's test_training_empty/test_training_detail_empty
        # assume zero sessions, and file execution order isn't guaranteed
        # (mirrors the cleanup in test_api.py's test_noporn_and_training_detail_with_data).
        conn.execute("DELETE FROM training_entries WHERE session_id = ?", (sess_id,))
        conn.execute("DELETE FROM training_sessions WHERE id = ?", (sess_id,))
        conn.execute("DELETE FROM training_exercises WHERE id = ?", (ex_id,))
        conn.commit()
        conn.close()
