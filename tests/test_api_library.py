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


def test_library_requires_key(client):
    assert client.get("/api/library").status_code == 401


def test_list_returns_crossfit_vocabulary(auth_client):
    body = auth_client.get("/api/library", headers=KEY, params={"category": "CrossFit"}).json()
    names = {e["name"] for e in body["entries"]}
    assert "Thruster" in names
    assert len(body["entries"]) == 31
    assert all(e["category"] == "CrossFit" for e in body["entries"])


def test_create_then_read_back(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"category": "CrossFit", "section": "Core", "name": "Devil Press", "metric": "reps"},
    )
    assert resp.status_code == 201
    row = _get("Devil Press")
    assert row["section"] == "Core"
    assert row["metric"] == "reps"
    assert row["builtin"] == 0


def test_duplicate_name_in_category_is_409(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"category": "CrossFit", "section": "Core", "name": "Thruster", "metric": "reps"},
    )
    assert resp.status_code == 409


def test_patch_edits_fields(auth_client):
    entry_id = _get("Devil Press")["id"]
    resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"notes": "dumbbell, brutal"})
    assert resp.status_code == 200
    assert _get("Devil Press")["notes"] == "dumbbell, brutal"


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


def test_builtin_row_can_still_be_archived(auth_client):
    row = _get("Goblet Squat")
    assert auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 1}).status_code == 200
    assert _get("Goblet Squat")["archived"] == 1
    auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"archived": 0})


def test_wod_entries_surface_in_training_reads(auth_client):
    """Entries written against an ad_hoc movement must appear in the machine-facing
    reads, not just the UI — this is what an MCP client sees."""
    conn = sqlite3.connect(user_db_path())
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
    finally:
        conn.close()

    detail = auth_client.get("/api/training/detail", headers=KEY, params={"range": 7}).json()
    blob = str(detail)
    assert "Ski Erg" in blob, "ad_hoc movements must be visible to API/MCP consumers"
    assert "ski erg intervals" in blob, "the raw WOD note must reach API consumers"
