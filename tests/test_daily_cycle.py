"""Changing one daily status in place (the dashboard's Today block)."""

import sqlite3

from conftest import csrf_token, user_db_path

DAY = "2026-03-14"


def _row() -> dict:
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM daily_logs WHERE date = ?", (DAY,)).fetchone())
    finally:
        conn.close()


def _cycle(client, field: str):
    token = csrf_token(client, "/")
    return client.post(f"/daily/{DAY}/cycle", data={"field": field}, headers={"X-CSRF-Token": token, "HX-Request": "true"})


def test_a_tap_advances_one_status_and_keeps_the_rest_of_the_day(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM daily_logs WHERE date = ?", (DAY,))
        conn.execute(
            "INSERT INTO daily_logs (date, energy, andy_body_desc, notes, water) VALUES (?, 7, 'Zone-2 walk', 'slept well', 'done')",
            (DAY,),
        )
        conn.commit()
    finally:
        conn.close()

    seen = []
    for _ in range(3):
        resp = _cycle(auth_client, "andy_body_status")
        assert resp.status_code == 200
        assert 'id="today-block"' in resp.text, "the block is redrawn so its count stays true"
        seen.append(_row()["andy_body_status"])
    assert seen == ["done", "skipped", "pending"], "the daily page's order"

    # /daily/save would have reset these: the tapping page never posts them.
    row = _row()
    assert (row["energy"], row["andy_body_desc"], row["notes"], row["water"]) == (7, "Zone-2 walk", "slept well", "done")


def test_a_tap_on_an_unlogged_day_starts_it(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM daily_logs WHERE date = ?", (DAY,))
        conn.commit()
    finally:
        conn.close()

    assert _cycle(auth_client, "morning_routine").status_code == 200
    assert _row()["morning_routine"] == "done"


def test_only_status_fields_can_be_cycled(auth_client):
    resp = _cycle(auth_client, "notes")
    assert resp.status_code == 422
    assert "not a daily-log status" in resp.headers["X-Feedback-Message"]
