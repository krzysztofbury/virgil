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
    now always returns the whole library. The expected count is DERIVED from
    the seed data, not hardcoded, so it stays correct (and exact) if
    EXERCISE_LIBRARY or CROSSFIT_MOVEMENTS ever changes: every seeded name,
    deduped the same way migration 019 dedupes them (stripped,
    case-insensitive) — the four known collisions (Back Squat, Deadlift,
    Bench Press, Pull-up) collapse automatically via the set."""
    from app.exercise_library import CROSSFIT_MOVEMENTS, EXERCISE_LIBRARY

    expected_names = {e["name"].strip().lower() for e in EXERCISE_LIBRARY} | {
        m["name"].strip().lower() for m in CROSSFIT_MOVEMENTS
    }

    body = auth_client.get("/api/library", headers=KEY).json()
    names = {e["name"] for e in body["entries"]}
    assert "Thruster" in names
    assert {m["name"] for m in CROSSFIT_MOVEMENTS} <= names, "every CrossFit movement must still be listed"
    assert len(body["entries"]) == len(expected_names)


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


def test_duplicate_name_unicode_case_insensitive_is_409(auth_client):
    """SQLite's lower()/COLLATE NOCASE are ASCII-only. 'ĆWICZENIE' vs
    'ćwiczenie' is the case that actually proves it: SQL lower('ĆWICZENIE')
    returns 'Ćwiczenie' (only the ASCII letters fold; the leading Ć does
    not), which does not equal 'ćwiczenie' — so this pair is rejected by
    validate_library_write's Python-side check but would sail past a SQL
    `lower(name) = lower(?)` comparison. (A pair like 'Podciąganie' /
    'podciąganie' would NOT prove this: the only case-differing letter there
    is the ASCII 'P', which SQL's lower() folds correctly on its own.)"""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "ĆWICZENIE", "metric": "reps"},
    )
    assert resp.status_code == 201
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.post(
            "/api/library",
            headers=KEY,
            json={"section": "Core", "name": "ćwiczenie", "metric": "reps"},
        )
        assert resp.status_code == 409
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


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


def test_patch_rename_collision_case_insensitive_409(auth_client):
    """The rename-collision check must be case-insensitive, same as create's
    dup check — a bare `name = ?` would let a PATCH rename onto a
    case-variant of an existing name reach the DB's UNIQUE(name COLLATE
    NOCASE) constraint directly (a 500), instead of this clean 409."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Rename Case Collision Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"name": "thruster"})
        assert resp.status_code == 409
        assert _get("Rename Case Collision Target") is not None
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


def test_create_with_tags_and_read_back(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Sled Push", "metric": "reps", "tags": ["HYROX", "conditioning"]},
    )
    assert resp.status_code == 201
    body = auth_client.get("/api/library", headers=KEY, params={"tag": "hyrox"}).json()
    names = {e["name"] for e in body["entries"]}
    assert "Sled Push" in names
    entry = next(e for e in body["entries"] if e["name"] == "Sled Push")
    assert entry["tags"] == ["conditioning", "hyrox"], "tags come back normalised and sorted"


def test_category_is_rejected_now_that_it_is_gone(auth_client):
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Ghost Move", "category": "CrossFit", "metric": "reps"},
    )
    assert resp.status_code == 422, "extra='forbid' must reject the retired field"


def test_patch_replaces_tags(auth_client):
    entry_id = _get("Sled Push")["id"]
    assert auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"tags": ["hyrox"]}).status_code == 200
    body = auth_client.get("/api/library", headers=KEY, params={"tag": "conditioning"}).json()
    assert "Sled Push" not in {e["name"] for e in body["entries"]}


def test_patch_without_tags_field_leaves_existing_tags_alone(auth_client):
    """Absence of `tags` in a PATCH body must mean 'leave them', never 'clear them'.
    api_library_patch's `fields.pop("tags", None)` vs `fields.pop("tags", [])` is the
    entire difference between those two outcomes -- a PATCH that never mentions tags
    at all must not silently wipe them."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Tag Survival Target", "metric": "reps", "tags": ["kettlebell"]},
    )
    entry_id = resp.json()["id"]
    try:
        assert auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"notes": "x"}).status_code == 200
        body = auth_client.get("/api/library", headers=KEY, params={"tag": "kettlebell"}).json()
        assert "Tag Survival Target" in {e["name"] for e in body["entries"]}, (
            "a PATCH that doesn't mention tags must not wipe them"
        )
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_builtin_row_can_be_tagged(auth_client):
    """builtin protects name/section/metric, never tags."""
    row = _get("Goblet Squat")
    assert row["builtin"] == 1, "fixture assumption"
    assert auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"tags": ["kettlebell"]}).status_code == 200
    body = auth_client.get("/api/library", headers=KEY, params={"tag": "kettlebell"}).json()
    assert "Goblet Squat" in {e["name"] for e in body["entries"]}
    resp = auth_client.patch(f"/api/library/{row['id']}", headers=KEY, json={"name": "Hacked Squat"})
    assert resp.status_code == 409, "builtin still refuses name/section/metric changes, even after a tag edit"


def test_patch_rejects_combined_guarded_field_and_tags_on_builtin_row(auth_client):
    """Tags are not gated by builtin on their own (test_builtin_row_can_be_tagged above),
    but a PATCH that ALSO touches a frozen field (name here) on the same builtin row must
    be rejected wholesale — neither half may land. `fields` (which still contains `name`
    after `tags` is popped out) reaches validate_library_write's builtin guard exactly as
    it would for a name-only PATCH; that guard raises before the tags branch further down
    ever runs, so mixing `tags` into the same request as a frozen field is refused in
    full, not partially applied. Same ordering as the settings form's library_update."""
    row = _get("Goblet Squat")
    resp = auth_client.patch(
        f"/api/library/{row['id']}",
        headers=KEY,
        json={"name": "Hacked Combo Squat", "tags": ["sneaky-combo"]},
    )
    assert resp.status_code == 409
    assert _get("Goblet Squat") is not None, "name must not change on a rejected combined PATCH"
    assert _get("Hacked Combo Squat") is None
    body = auth_client.get("/api/library", headers=KEY, params={"tag": "sneaky-combo"}).json()
    assert body["entries"] == [], "tags must not land on a rejected combined PATCH either"


def test_tag_filter_is_case_insensitive(auth_client):
    """?tag= must be normalised the same way a write is -- a caller filtering with
    different case than how the tag was stored (tags are always stored lowercased)
    must still match."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Case Filter Target", "metric": "reps", "tags": ["kettlebell"]},
    )
    entry_id = resp.json()["id"]
    try:
        body = auth_client.get("/api/library", headers=KEY, params={"tag": "Kettlebell"}).json()
        assert "Case Filter Target" in {e["name"] for e in body["entries"]}
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_tags_by_library_id_sorts_regardless_of_row_order():
    """_tags_by_library_id's `sorted()` is unreachable through the real API/DB path:
    exercise_library_tags's composite PRIMARY KEY (library_id, tag) makes SQLite
    satisfy `WHERE library_id IN (...)` with a covering-index scan over that same
    (library_id, tag) index (confirmed with EXPLAIN QUERY PLAN), which hands back
    rows already ordered by tag -- so a real-DB test inserting tags in any order
    still gets them back sorted with or without this function's own sort. This
    test stubs the db dependency to hand back genuinely out-of-order rows, so it
    exercises `sorted()` itself rather than SQLite's incidental ordering."""
    import asyncio

    from app.routers.api import _tags_by_library_id

    class FakeDb:
        async def execute_fetchall(self, sql, params):
            return [
                {"library_id": 1, "tag": "zeta"},
                {"library_id": 1, "tag": "alpha"},
                {"library_id": 1, "tag": "mid"},
            ]

    result = asyncio.run(_tags_by_library_id(FakeDb(), [1]))
    assert result == {1: ["alpha", "mid", "zeta"]}


def test_patch_with_invalid_tag_applies_no_field(auth_client):
    """A rejected tag must not let another field in the same PATCH slip through --
    same invariant test_patch_rejects_unknown_field pins for an unknown field, now for
    a validation failure that happens AFTER the SQL-column fields already validated
    (tags normalise, and can therefore still reject, after `result` is computed)."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Partial Patch Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]
    try:
        resp = auth_client.patch(f"/api/library/{entry_id}", headers=KEY, json={"notes": "ok", "tags": ["!!!"]})
        assert resp.status_code == 422
        assert _get("Partial Patch Target")["notes"] == "", "a rejected PATCH must not partially apply"
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_patch_update_does_not_write_before_tags_validate(auth_client):
    """M2 (2026-07-31 review): api_library_patch used to run the column UPDATE
    BEFORE normalize_tags got a chance to raise, with no db.rollback() in the
    except branch. The test above (test_patch_with_invalid_tag_applies_no_field)
    can't tell a real fix from accidental safety: auth.py opens a brand new
    connection per request and closes it in a `finally` without ever
    committing on the exception path, so an uncommitted UPDATE disappears
    when THAT connection closes regardless of whether the handler itself
    validates in the right order — it would pass identically before and
    after this fix. This test calls the handler directly on a connection WE
    keep open so we can inspect the row through the SAME connection before
    any rollback/close: if the UPDATE ran, its (uncommitted) effect is
    already visible here, with or without a subsequent rollback."""
    import asyncio

    import aiosqlite
    from fastapi import HTTPException

    from app.routers.api import LibraryPatch, api_library_patch

    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "M2 Same-Connection Target", "metric": "reps"},
    )
    entry_id = resp.json()["id"]

    async def scenario():
        db = await aiosqlite.connect(user_db_path())
        db.row_factory = aiosqlite.Row
        try:
            raised = False
            try:
                await api_library_patch(db, entry_id, LibraryPatch(notes="should not stick", tags=["!!!"]))
            except HTTPException as exc:
                raised = True
                assert exc.status_code == 422
            assert raised, "the bad tag must still be rejected"
            # Same connection, no commit/rollback issued yet by us -- an
            # uncommitted UPDATE would already be visible here if the handler
            # had run it before raising.
            row = await db.execute_fetchall("SELECT notes FROM exercise_library WHERE id = ?", (entry_id,))
            return row[0]["notes"]
        finally:
            await db.rollback()
            await db.close()

    notes = asyncio.run(scenario())
    try:
        assert notes == "", (
            "the UPDATE must not run before tag validation -- an uncommitted write must never be "
            "issued for a request that ends up rejected, regardless of what closes the connection"
        )
    finally:
        auth_client.delete(f"/api/library/{entry_id}", headers=KEY)


def test_duplicate_name_is_409_regardless_of_tags(auth_client):
    """Beyond the plain 409 (test_duplicate_name_is_409 already covers that): a
    rejected duplicate create must not leak its tags onto the row it collided with --
    duplicate detection runs on `name` alone, before tags are ever normalised or
    written, so a successful attacker-style contamination would be a distinct bug."""
    resp = auth_client.post(
        "/api/library",
        headers=KEY,
        json={"section": "Core", "name": "Thruster", "metric": "reps", "tags": ["whatever"]},
    )
    assert resp.status_code == 409, "names are unique library-wide now"
    body = auth_client.get("/api/library", headers=KEY, params={"tag": "whatever"}).json()
    assert body["entries"] == [], "a rejected duplicate must not attach its tags to the existing row"
