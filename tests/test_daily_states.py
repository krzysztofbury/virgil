"""Daily: a state must be readable as a word, and the day must state its total.

The tri-state button showed a colour and an icon. A yellow minus does not say
"skipped", and nothing on the page said how much of the day was done.
"""

import sqlite3

from conftest import user_db_path

PROBE_DATE = "2026-08-19"


def _seed_log(**cols):
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("INSERT OR IGNORE INTO daily_logs (date) VALUES (?)", (PROBE_DATE,))
        if cols:
            assignments = ", ".join(f"{key} = ?" for key in cols)
            conn.execute(f"UPDATE daily_logs SET {assignments} WHERE date = ?", (*cols.values(), PROBE_DATE))
        conn.commit()
    finally:
        conn.close()


def test_states_are_words_and_carry_aria_pressed(auth_client):
    _seed_log(morning_routine="done", evening_routine="skipped", water="pending")
    html = auth_client.get(f"/daily/{PROBE_DATE}").text
    assert 'class="toggle-state"' in html, "each toggle must render its state as a word"
    assert ">Done<" in html and ">Skipped<" in html and ">Pending<" in html
    assert 'aria-pressed="true"' in html and 'aria-pressed="false"' in html


def test_completion_count_is_stated(auth_client):
    _seed_log(
        morning_routine="done",
        evening_routine="done",
        water="pending",
        andy_body_status="done",
        andy_spirit_status="pending",
        andy_account_status="skipped",
        andy_relations_status="pending",
    )
    html = auth_client.get(f"/daily/{PROBE_DATE}").text
    assert 'id="done-count"' in html
    assert "3/7" in html, "three of seven states are done"


def test_a_finished_task_reads_as_content(auth_client):
    """A done task with a description is text plus an Edit disclosure, not a
    permanent input the user has to look past."""
    _seed_log(andy_body_status="done", andy_body_desc="30 min bike")
    html = auth_client.get(f"/daily/{PROBE_DATE}").text
    body_block = html[html.index("andy_body_status") :]
    body_block = body_block[: body_block.index("andy_spirit_status")]
    assert "30 min bike" in body_block
    assert "<summary>Edit</summary>" in body_block, "editing a finished task is on demand"
    assert 'name="andy_body_desc"' in body_block, "the input must still submit inside the disclosure"


def test_trends_are_grouped_and_hold_no_canvas(auth_client):
    html = auth_client.get(f"/daily/{PROBE_DATE}").text
    start = html.index('id="daily-trends"')
    block = html[start : html.index("</details>", start)]
    assert "<canvas" not in block
    assert "Completion Heatmap" in block or "heatmap-grid" in block


def test_energy_has_semantic_anchors(auth_client):
    html = auth_client.get(f"/daily/{PROBE_DATE}").text
    assert "energy-anchors" in html
    for word in ("Low", "OK", "High"):
        assert f">{word}<" in html
