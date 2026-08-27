"""Goals: a soft focus set, one horizon at a time, one add flow.

The page used to render every area against every horizon, so the empty state was
24 identical inputs and about 3356 px on a phone. The focus set is advisory by
decision: it warns above three goals and blocks nothing, so no existing goal can
be lost to a cap.
"""

import re
import sqlite3

from conftest import csrf_token, user_db_path

from app.routers.goals import FOCUS_SOFT_LIMIT


def _area_id():
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT id FROM goal_areas ORDER BY display_order LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _add_goal(content, horizon="1yr", active=0):
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO goals (area_id, horizon, content, active) VALUES (?, ?, ?, ?)",
            (_area_id(), horizon, content, active),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _is_active(goal_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT active FROM goals WHERE id = ?", (goal_id,)).fetchone()[0]
    finally:
        conn.close()


def test_migration_adds_the_active_column(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(goals)")}
    finally:
        conn.close()
    assert "active" in cols


def test_toggle_focus_flips_the_flag_both_ways(auth_client):
    goal_id = _add_goal("ZZ focus probe")
    token = csrf_token(auth_client, "/goals")

    first = auth_client.post(
        "/goals/toggle-focus", data={"_csrf_token": token, "goal_id": goal_id}, follow_redirects=False
    )
    assert first.status_code == 303
    assert _is_active(goal_id) == 1

    auth_client.post("/goals/toggle-focus", data={"_csrf_token": token, "goal_id": goal_id}, follow_redirects=False)
    assert _is_active(goal_id) == 0


def test_focus_is_soft_and_warns_above_the_limit(auth_client):
    token = csrf_token(auth_client, "/goals")
    ids = [_add_goal(f"ZZ soft focus {i}") for i in range(FOCUS_SOFT_LIMIT + 1)]
    for goal_id in ids:
        resp = auth_client.post(
            "/goals/toggle-focus", data={"_csrf_token": token, "goal_id": goal_id}, follow_redirects=False
        )
        assert resp.status_code == 303, "the focus set must refuse nothing"
    assert all(_is_active(goal_id) == 1 for goal_id in ids), "no goal may be dropped by a cap"

    html = auth_client.get("/goals").text
    assert "Current focus" in html
    assert f"{len(ids)} goals starred" in html, "above the limit the page must say so"
    assert "ZZ soft focus 0" in html


def test_page_shows_one_horizon_and_one_add_flow(auth_client):
    _add_goal("ZZ one year goal", horizon="1yr")
    _add_goal("ZZ ten year goal", horizon="10yr")

    default = auth_client.get("/goals").text
    assert "ZZ one year goal" in default
    assert "ZZ ten year goal" not in default, "only the selected horizon is listed"

    ten = auth_client.get("/goals?horizon=10yr").text
    assert "ZZ ten year goal" in ten

    bad = auth_client.get("/goals?horizon=nonsense").text
    assert "ZZ one year goal" in bad, "an unknown horizon falls back to 1yr"

    # One add flow that asks for area and horizon, instead of one input per pair.
    assert default.count("New goal...") == 1, "the 24-input empty state must be gone"
    assert 'name="area_id"' in default and 'name="horizon"' in default


def test_empty_areas_stay_compact(auth_client):
    _add_goal("ZZ three year goal", horizon="3yr")
    html = auth_client.get("/goals?horizon=3yr").text
    assert re.search(r'class="goal-area-empty"', html), "areas with nothing in this horizon must render compact"
    assert "New goal..." in html, "the one add flow stays available"
