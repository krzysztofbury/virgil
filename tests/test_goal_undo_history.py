"""A deleted goal comes back for a while, and each goal keeps a short history.

The history says what happened and which fields changed, never their values:
the goal's text lives in one place, and a deleted goal's copy goes when it ages
out.
"""

import asyncio
import re
import sqlite3

import aiosqlite
import pytest

from app.migrations.runner import run_migrations
from app.services.goal_data import (
    GOAL_HISTORY_MAX,
    GoalDataError,
    create_goal,
    delete_goal_record,
    goal_history,
    list_deleted_goals,
    restore_goal_record,
    update_goal,
)
from tests.conftest import csrf_token, user_db_path


def _run(tmp_path, scenario):
    async def wrapper():
        db = await aiosqlite.connect(tmp_path / "goals.db")
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        try:
            await run_migrations(db)
            area = (await db.execute_fetchall("SELECT id FROM goal_areas ORDER BY id LIMIT 1"))[0]["id"]
            return await scenario(db, area)
        finally:
            await db.close()

    return asyncio.run(wrapper())


def _events(history: list[dict]) -> list[tuple]:
    return [(c["event"], c["fields"], c["status_from"], c["status_to"]) for c in reversed(history)]


def test_a_restored_goal_comes_back_whole_with_the_links_its_delete_cleared(tmp_path):
    async def scenario(db, area):
        parent, _ = await create_goal(db, area_id=area, horizon="3yr", content="Run a marathon", active=1)
        child, _ = await create_goal(
            db, area_id=area, horizon="1yr", content="Half marathon", parent_goal_id=parent["id"]
        )
        await db.execute(
            "INSERT INTO experiments (title, start_date, num_weeks, goal_id) VALUES ('Zone 2', '2026-10-01', 4, ?)",
            (parent["id"],),
        )
        await delete_goal_record(db, parent["id"])
        gone = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (parent["id"],))
        unlinked = (await db.execute_fetchall("SELECT parent_goal_id FROM goals WHERE id = ?", (child["id"],)))[0][0]
        listed = await list_deleted_goals(db)
        restored = await restore_goal_record(db, parent["id"])
        relinked = (await db.execute_fetchall("SELECT parent_goal_id FROM goals WHERE id = ?", (child["id"],)))[0][0]
        experiment = (await db.execute_fetchall("SELECT goal_id FROM experiments"))[0][0]
        return parent, gone, unlinked, listed, restored, relinked, experiment, await list_deleted_goals(db)

    parent, gone, unlinked, listed, restored, relinked, experiment, after = _run(tmp_path, scenario)

    assert gone == [] and unlinked is None
    assert [(g["id"], g["content"]) for g in listed] == [(parent["id"], "Run a marathon")]
    assert restored == parent, "same id, text, horizon, focus and dates"
    assert relinked == parent["id"] and experiment == parent["id"]
    assert after == []


def test_a_restore_keeps_what_changed_while_the_goal_was_deleted(tmp_path):
    async def scenario(db, area):
        parent, _ = await create_goal(db, area_id=area, horizon="3yr", content="Parent")
        goal, _ = await create_goal(
            db,
            area_id=area,
            horizon="1yr",
            content="Imported",
            parent_goal_id=parent["id"],
            source="import",
            source_ref="k1",
        )
        await delete_goal_record(db, goal["id"])
        await delete_goal_record(db, parent["id"])
        await create_goal(db, area_id=area, horizon="1yr", content="Imported again", source="import", source_ref="k1")
        return await restore_goal_record(db, goal["id"])

    restored = _run(tmp_path, scenario)

    assert restored["parent_goal_id"] is None, "its parent is still deleted"
    assert restored["source_ref"] == "", "the newer goal keeps the idempotency key"


def test_a_goal_deleted_too_long_ago_cannot_be_restored_and_is_forgotten(tmp_path):
    async def scenario(db, area):
        old, _ = await create_goal(db, area_id=area, horizon="1yr", content="Old")
        await delete_goal_record(db, old["id"])
        await db.execute("UPDATE deleted_goals SET deleted_at = datetime('now', '-31 days')")
        listed = await list_deleted_goals(db)
        with pytest.raises(GoalDataError) as refused:
            await restore_goal_record(db, old["id"])
        recent, _ = await create_goal(db, area_id=area, horizon="1yr", content="Recent")
        await delete_goal_record(db, recent["id"])  # a delete also purges what aged out
        kept = await db.execute_fetchall("SELECT id FROM deleted_goals")
        history = await db.execute_fetchall("SELECT DISTINCT goal_id FROM goal_changes")
        return old, recent, listed, refused.value.status_code, kept, history

    old, recent, listed, status, kept, history = _run(tmp_path, scenario)

    assert listed == [] and status == 404
    assert [r["id"] for r in kept] == [recent["id"]]
    assert old["id"] not in [r["goal_id"] for r in history], "its history goes with it"


def test_a_goal_with_reps_is_still_refused_and_nothing_is_kept(tmp_path):
    async def scenario(db, area):
        goal, _ = await create_goal(db, area_id=area, horizon="1yr", content="Has reps")
        await db.execute(
            "INSERT INTO goal_reps (goal_id, content, period, period_start, period_end, due_date) "
            "VALUES (?, 'Rep', 'month', '2026-10-01', '2026-10-31', '2026-10-31')",
            (goal["id"],),
        )
        with pytest.raises(GoalDataError) as refused:
            await delete_goal_record(db, goal["id"])
        return refused.value.status_code, await db.execute_fetchall("SELECT 1 FROM deleted_goals")

    status, kept = _run(tmp_path, scenario)

    assert status == 409 and kept == []


def test_history_names_what_changed_but_never_the_text(tmp_path):
    async def scenario(db, area):
        goal, _ = await create_goal(db, area_id=area, horizon="1yr", content="Secret first wording")
        await update_goal(db, goal["id"], {"content": "Secret second wording"})
        await update_goal(db, goal["id"], {"content": "Secret second wording"})  # no change, no entry
        await update_goal(db, goal["id"], {"status": "completed", "end_date": "2026-12-31"})
        await update_goal(db, goal["id"], {"status": "active"})
        await update_goal(db, goal["id"], {"active": 1})
        stored = await db.execute_fetchall("SELECT * FROM goal_changes")
        return (await goal_history(db, [goal["id"]]))[goal["id"]], [tuple(row) for row in stored]

    history, stored = _run(tmp_path, scenario)

    assert _events(history) == [
        ("created", "", None, "active"),
        ("edited", "text", None, None),
        ("edited", "status, end date", "active", "completed"),
        ("edited", "status", "completed", "active"),
        ("focused", "", None, None),
    ]
    assert not any("Secret" in str(value) for row in stored for value in row)
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", c["when"]) for c in history)


def test_history_is_bounded_per_goal(tmp_path):
    async def scenario(db, area):
        goal, _ = await create_goal(db, area_id=area, horizon="1yr", content="Busy")
        for n in range(GOAL_HISTORY_MAX + 5):
            await update_goal(db, goal["id"], {"display_order": n + 1})
        return (await goal_history(db, [goal["id"]]))[goal["id"]]

    history = _run(tmp_path, scenario)

    assert len(history) == GOAL_HISTORY_MAX
    assert history[0]["event"] == "edited" and "created" not in [c["event"] for c in history], "the oldest go first"


def test_the_goals_page_deletes_restores_and_shows_changes(auth_client):
    conn = sqlite3.connect(user_db_path())
    area = conn.execute("SELECT id FROM goal_areas ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()
    token = csrf_token(auth_client, "/goals")
    auth_client.post(
        "/goals/save", data={"_csrf_token": token, "area_id": area, "horizon": "10yr", "content": "ZZ undo me"}
    )
    conn = sqlite3.connect(user_db_path())
    goal_id = conn.execute("SELECT id FROM goals WHERE content = 'ZZ undo me'").fetchone()[0]
    conn.close()
    try:
        auth_client.post("/goals/toggle-focus", data={"_csrf_token": token, "goal_id": goal_id, "horizon": "10yr"})
        deleted = auth_client.post(
            "/goals/delete", data={"_csrf_token": token, "goal_id": goal_id, "horizon": "10yr"}, follow_redirects=True
        ).text
        restored = auth_client.post(
            "/goals/restore", data={"_csrf_token": token, "goal_id": goal_id, "horizon": "1yr"}, follow_redirects=True
        ).text
    finally:
        conn = sqlite3.connect(user_db_path())
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
        conn.execute("DELETE FROM deleted_goals WHERE id = ?", (goal_id,))
        conn.execute("DELETE FROM goal_changes WHERE goal_id = ?", (goal_id,))
        conn.commit()
        conn.close()

    trash = deleted[deleted.index('id="recently-deleted"') :]
    assert "ZZ undo me" in trash[: trash.index("</details>")]
    assert "Restore it under Recently deleted for 30 days." in deleted
    assert "Goal restored." in restored and 'value="ZZ undo me"' not in restored  # a textarea, not an input
    assert ">ZZ undo me</textarea>" in restored, "back on its own horizon"
    changes = restored[restored.index(">ZZ undo me</textarea>") :]
    changes = changes[changes.index('<div class="goal-changes">') : changes.index("</ol>")]
    items = [re.sub(r"\s+", " ", item).strip() for item in re.findall(r"</time>(.*?)</li>", changes, flags=re.S)]
    assert items == ["Restored", "Deleted", "Added to focus", "Created"]
