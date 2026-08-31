import asyncio
import sqlite3
from datetime import date, timedelta
from urllib.parse import urlsplit

import aiosqlite
import pytest
from conftest import csrf_token, user_db_path

from app.services.goal_data import GoalDataError, create_rep, delete_goal_record, update_goal

KEY = {"X-API-Key": "test-key-123"}


def _area_id() -> int:
    with sqlite3.connect(user_db_path()) as conn:
        return conn.execute("SELECT id FROM goal_areas ORDER BY display_order LIMIT 1").fetchone()[0]


def _cleanup_goal(goal_id: int) -> None:
    with sqlite3.connect(user_db_path()) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))


def test_goal_and_rep_api_are_idempotent_and_expose_calendar_periods(auth_client):
    goal_payload = {
        "area_id": _area_id(),
        "horizon": "1yr",
        "content": "ZZ canonical API goal",
        "status": "active",
        "start_date": "2026-08-01",
        "end_date": "2026-12-31",
        "focus": True,
        "idempotency_key": "test-goal-canonical-1",
    }
    first = auth_client.post("/api/goals", json=goal_payload, headers=KEY)
    assert first.status_code == 200, first.text
    goal_id = first.json()["id"]
    try:
        second = auth_client.post("/api/goals", json=goal_payload, headers=KEY)
        assert second.status_code == 200
        assert second.json()["id"] == goal_id
        assert second.json()["created"] is False
        conflict_payload = {**goal_payload, "content": "ZZ different write with reused key"}
        conflict = auth_client.post("/api/goals", json=conflict_payload, headers=KEY)
        assert conflict.status_code == 409

        active = auth_client.get("/api/goals", params={"status": "active", "focus": True}, headers=KEY).json()
        goal = next(item for item in active["goals"] if item["id"] == goal_id)
        assert goal["start_date"] == "2026-08-01"
        assert goal["pending_reps"] == 0
        assert goal["experiments"] == []

        rep_payload = {
            "goal_id": goal_id,
            "content": "ZZ finish the calendar-week rep",
            "period": "week",
            "due_date": "2026-09-02",
            "idempotency_key": "test-goal-rep-1",
        }
        rep = auth_client.post("/api/goal-reps", json=rep_payload, headers=KEY)
        assert rep.status_code == 200, rep.text
        rep_body = rep.json()
        assert rep_body["period_start"] == "2026-08-31"
        assert rep_body["period_end"] == "2026-09-06"

        replay = auth_client.post("/api/goal-reps", json=rep_payload, headers=KEY).json()
        assert replay["id"] == rep_body["id"]
        assert replay["created"] is False
        rep_conflict = auth_client.post(
            "/api/goal-reps",
            json={**rep_payload, "content": "ZZ changed rep with reused key"},
            headers=KEY,
        )
        assert rep_conflict.status_code == 409
    finally:
        _cleanup_goal(goal_id)


def test_rep_complete_carry_skip_transitions_preserve_history(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ transition goal",
            "idempotency_key": "test-transition-goal",
        },
        headers=KEY,
    ).json()
    goal_id = goal["id"]
    try:
        rep = auth_client.post(
            "/api/goal-reps",
            json={
                "goal_id": goal_id,
                "content": "ZZ carry me",
                "period": "month",
                "due_date": "2026-08-31",
                "idempotency_key": "test-carry-source",
            },
            headers=KEY,
        ).json()
        carried = auth_client.post(
            f"/api/goal-reps/{rep['id']}/transition",
            json={"action": "carry", "due_date": "2026-09-30"},
            headers=KEY,
        )
        assert carried.status_code == 200, carried.text
        body = carried.json()
        assert body["rep"]["status"] == "carried"
        assert body["carried_to"]["status"] == "pending"
        assert body["carried_to"]["carried_from_id"] == rep["id"]
        carry_replay = auth_client.post(
            f"/api/goal-reps/{rep['id']}/transition",
            json={"action": "carry", "due_date": "2026-09-30"},
            headers=KEY,
        )
        assert carry_replay.status_code == 200
        assert carry_replay.json()["carried_to"]["id"] == body["carried_to"]["id"]
        assert auth_client.delete(f"/api/goal-reps/{body['carried_to']['id']}", headers=KEY).status_code == 409

        completed = auth_client.post(
            f"/api/goal-reps/{body['carried_to']['id']}/transition",
            json={"action": "complete"},
            headers=KEY,
        ).json()["rep"]
        assert completed["status"] == "completed"
        assert completed["completed_at"] is not None
        complete_replay = auth_client.post(
            f"/api/goal-reps/{body['carried_to']['id']}/transition",
            json={"action": "complete"},
            headers=KEY,
        )
        assert complete_replay.status_code == 200
        assert complete_replay.json()["rep"]["completed_at"] == completed["completed_at"]

        repeated = auth_client.post(
            f"/api/goal-reps/{rep['id']}/transition",
            json={"action": "skip"},
            headers=KEY,
        )
        assert repeated.status_code == 409

        history = auth_client.get("/api/goal-reps", params={"goal_id": goal_id, "limit": 2}, headers=KEY).json()
        assert len(history["reps"]) == 2
        assert {item["status"] for item in history["reps"]} == {"carried", "completed"}
    finally:
        _cleanup_goal(goal_id)


def test_completed_goal_is_timestamped_and_cannot_enter_focus(auth_client):
    response = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ completed lifecycle goal",
            "status": "completed",
            "focus": True,
            "idempotency_key": "test-completed-lifecycle-goal",
        },
        headers=KEY,
    )
    assert response.status_code == 200, response.text
    goal = response.json()
    try:
        assert goal["active"] == 0
        assert goal["completed_at"] is not None
        token = csrf_token(auth_client, "/goals")
        focused = auth_client.post(
            "/goals/toggle-focus",
            data={"_csrf_token": token, "goal_id": goal["id"]},
            follow_redirects=False,
        )
        assert "err=" in focused.headers["location"]
    finally:
        _cleanup_goal(goal["id"])


def test_goal_parent_links_reject_indirect_cycles(auth_client):
    first = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ parent cycle first",
            "idempotency_key": "test-parent-cycle-first",
        },
        headers=KEY,
    ).json()
    second = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ parent cycle second",
            "idempotency_key": "test-parent-cycle-second",
        },
        headers=KEY,
    ).json()
    try:
        linked = auth_client.patch(f"/api/goals/{first['id']}", json={"parent_goal_id": second["id"]}, headers=KEY)
        assert linked.status_code == 200
        cycle = auth_client.patch(f"/api/goals/{second['id']}", json={"parent_goal_id": first["id"]}, headers=KEY)
        assert cycle.status_code == 409
        unchanged = auth_client.get(f"/api/goals/{second['id']}", headers=KEY).json()
        assert unchanged["parent_goal_id"] is None
    finally:
        _cleanup_goal(first["id"])
        _cleanup_goal(second["id"])


class _PausingDb:
    def __init__(self, db, *, select_prefix: str = "", execute_prefix: str = ""):
        self.db = db
        self.select_prefix = select_prefix
        self.execute_prefix = execute_prefix
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()
        self.did_pause = False

    async def execute_fetchall(self, sql, parameters=()):
        rows = await self.db.execute_fetchall(sql, parameters)
        if not self.did_pause and self.select_prefix and sql.startswith(self.select_prefix):
            self.did_pause = True
            self.paused.set()
            await asyncio.wait_for(self.resume.wait(), timeout=2)
        return rows

    async def execute(self, sql, parameters=()):
        if not self.did_pause and self.execute_prefix and sql.startswith(self.execute_prefix):
            self.did_pause = True
            self.paused.set()
            await asyncio.wait_for(self.resume.wait(), timeout=2)
        return await self.db.execute(sql, parameters)


async def _open_goal_test_db():
    db = await aiosqlite.connect(user_db_path())
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


def test_pending_rep_creation_wins_race_against_goal_closure(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ close race goal",
            "idempotency_key": "test-close-race-goal",
        },
        headers=KEY,
    ).json()

    async def scenario():
        close_db = await _open_goal_test_db()
        rep_db = await _open_goal_test_db()
        paused_db = _PausingDb(close_db, select_prefix="SELECT 1 FROM goal_reps WHERE goal_id")
        try:
            closing = asyncio.create_task(update_goal(paused_db, goal["id"], {"status": "completed"}))
            await asyncio.wait_for(paused_db.paused.wait(), timeout=2)
            await create_rep(
                rep_db,
                goal_id=goal["id"],
                content="ZZ concurrent pending rep",
                period="month",
                due_date="2026-09-30",
            )
            await rep_db.commit()
            paused_db.resume.set()
            with pytest.raises(GoalDataError, match="pending reps"):
                await asyncio.wait_for(closing, timeout=2)
            await close_db.rollback()
        finally:
            await close_db.close()
            await rep_db.close()

    try:
        asyncio.run(scenario())
        with sqlite3.connect(user_db_path()) as conn:
            state = conn.execute("SELECT status FROM goals WHERE id = ?", (goal["id"],)).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM goal_reps WHERE goal_id = ? AND status = 'pending'", (goal["id"],)
            ).fetchone()[0]
        assert (state, pending) == ("active", 1)
    finally:
        _cleanup_goal(goal["id"])


def test_pending_rep_creation_wins_race_against_goal_delete(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ delete race goal",
            "idempotency_key": "test-delete-race-goal",
        },
        headers=KEY,
    ).json()

    async def scenario():
        delete_db = await _open_goal_test_db()
        rep_db = await _open_goal_test_db()
        paused_db = _PausingDb(delete_db, execute_prefix="DELETE FROM goals")
        try:
            deleting = asyncio.create_task(delete_goal_record(paused_db, goal["id"]))
            await asyncio.wait_for(paused_db.paused.wait(), timeout=2)
            await create_rep(
                rep_db,
                goal_id=goal["id"],
                content="ZZ concurrent delete rep",
                period="month",
                due_date="2026-09-30",
            )
            await rep_db.commit()
            paused_db.resume.set()
            with pytest.raises(GoalDataError, match="execution history"):
                await asyncio.wait_for(deleting, timeout=2)
            await delete_db.rollback()
        finally:
            await delete_db.close()
            await rep_db.close()

    try:
        asyncio.run(scenario())
        with sqlite3.connect(user_db_path()) as conn:
            assert conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal["id"],)).fetchone() == (1,)
            assert conn.execute("SELECT COUNT(*) FROM goal_reps WHERE goal_id = ?", (goal["id"],)).fetchone()[0] == 1
    finally:
        _cleanup_goal(goal["id"])


def test_browser_can_create_goal_rep_and_complete_it(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ browser rep goal",
            "idempotency_key": "test-browser-rep-goal",
        },
        headers=KEY,
    ).json()
    try:
        token = csrf_token(auth_client, "/goals")
        due = (date.today() + timedelta(days=5)).isoformat()
        created = auth_client.post(
            "/goals/reps/save",
            data={
                "_csrf_token": token,
                "goal_id": goal["id"],
                "content": "ZZ browser one-off rep",
                "period": "week",
                "due_date": due,
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        with sqlite3.connect(user_db_path()) as conn:
            rep_id = conn.execute(
                "SELECT id FROM goal_reps WHERE goal_id = ? AND content = 'ZZ browser one-off rep'", (goal["id"],)
            ).fetchone()[0]

        html = auth_client.get("/goals").text
        assert "ZZ browser one-off rep" in html
        completed = auth_client.post(
            f"/goals/reps/{rep_id}/transition",
            data={"_csrf_token": token, "action": "complete"},
            follow_redirects=False,
        )
        assert completed.status_code == 303
        with sqlite3.connect(user_db_path()) as conn:
            assert conn.execute("SELECT status FROM goal_reps WHERE id = ?", (rep_id,)).fetchone()[0] == "completed"
    finally:
        _cleanup_goal(goal["id"])


def test_goal_details_do_not_revert_inline_content_or_display_order(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ original details content",
            "idempotency_key": "test-goal-details-partial-update",
        },
        headers=KEY,
    ).json()
    try:
        with sqlite3.connect(user_db_path()) as conn:
            conn.execute("UPDATE goals SET display_order = 17 WHERE id = ?", (goal["id"],))
        token = csrf_token(auth_client, "/goals")
        inline = auth_client.post(
            "/goals/update-inline",
            data={"_csrf_token": token, "goal_id": goal["id"], "content": "ZZ newer inline content"},
        )
        assert inline.status_code == 200
        details = auth_client.post(
            "/goals/details",
            data={
                "_csrf_token": token,
                "goal_id": goal["id"],
                "horizon": "1yr",
                "status": "paused",
                "start_date": "",
                "end_date": "",
            },
            follow_redirects=False,
        )
        assert details.status_code == 303
        with sqlite3.connect(user_db_path()) as conn:
            saved = conn.execute(
                "SELECT content, display_order, status FROM goals WHERE id = ?", (goal["id"],)
            ).fetchone()
        assert saved == ("ZZ newer inline content", 17, "paused")
    finally:
        _cleanup_goal(goal["id"])


def test_goal_cannot_close_or_delete_while_a_pending_rep_exists(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ protected execution history",
            "idempotency_key": "test-goal-pending-rep-protection",
        },
        headers=KEY,
    ).json()
    rep = auth_client.post(
        "/api/goal-reps",
        json={
            "goal_id": goal["id"],
            "content": "ZZ unresolved commitment",
            "period": "month",
            "due_date": "2026-09-30",
            "idempotency_key": "test-protected-pending-rep",
        },
        headers=KEY,
    ).json()
    try:
        for terminal_status in ("completed", "abandoned"):
            closed = auth_client.patch(f"/api/goals/{goal['id']}", json={"status": terminal_status}, headers=KEY)
            assert closed.status_code == 409
        assert auth_client.delete(f"/api/goals/{goal['id']}", headers=KEY).status_code == 409

        removed = auth_client.delete(f"/api/goal-reps/{rep['id']}", headers=KEY)
        assert removed.status_code == 200
        closed = auth_client.patch(f"/api/goals/{goal['id']}", json={"status": "completed"}, headers=KEY)
        assert closed.status_code == 200
    finally:
        _cleanup_goal(goal["id"])


def test_experiment_can_link_to_goal_and_duplicate_metric_names_are_refused(auth_client):
    goal = auth_client.post(
        "/api/goals",
        json={
            "area_id": _area_id(),
            "horizon": "1yr",
            "content": "ZZ linked experiment goal",
            "idempotency_key": "test-linked-experiment-goal",
        },
        headers=KEY,
    ).json()
    experiment_id = None
    try:
        token = csrf_token(auth_client, "/experiments/new")
        base = {
            "title": "ZZ linked experiment",
            "start_date": date.today().isoformat(),
            "num_weeks": "2",
            "goal_id": str(goal["id"]),
            "metric_names": ["Gate"],
            "metric_colors": ["#22c55e"],
            "metric_kinds": ["count"],
            "metric_targets": ["1"],
            "metric_periods": ["week"],
            "source_matches": [""],
            "_csrf_token": token,
        }
        created = auth_client.post("/experiments/create", data=base, follow_redirects=False)
        assert created.status_code == 303
        experiment_id = int(urlsplit(created.headers["location"]).path.rsplit("/", 1)[1])
        with sqlite3.connect(user_db_path()) as conn:
            assert (
                conn.execute("SELECT goal_id FROM experiments WHERE id = ?", (experiment_id,)).fetchone()[0]
                == goal["id"]
            )
        closed = auth_client.patch(f"/api/goals/{goal['id']}", json={"status": "completed"}, headers=KEY)
        assert closed.status_code == 200
        edit_html = auth_client.get(f"/experiments/{experiment_id}/edit").text
        assert "ZZ linked experiment goal (completed)" in edit_html

        duplicate = auth_client.post(
            "/experiments/create",
            data={
                **base,
                "title": "ZZ duplicate metric probe",
                "metric_names": ["Gate", "gate"],
                "metric_colors": ["#22c55e", "#3b82f6"],
                "metric_kinds": ["count", "count"],
                "metric_targets": ["1", "1"],
                "metric_periods": ["week", "week"],
                "source_matches": ["", ""],
            },
            follow_redirects=False,
        )
        assert duplicate.status_code == 303
        assert "err=" in duplicate.headers["location"]
        with sqlite3.connect(user_db_path()) as conn:
            assert (
                conn.execute("SELECT COUNT(*) FROM experiments WHERE title = 'ZZ duplicate metric probe'").fetchone()[0]
                == 0
            )
    finally:
        if experiment_id is not None:
            with sqlite3.connect(user_db_path()) as conn:
                conn.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
        _cleanup_goal(goal["id"])
