"""Daily: a state must be readable as a word, and the day must state its total.

The tri-state button showed a colour and an icon. A yellow minus does not say
"skipped", and nothing on the page said how much of the day was done.
"""

import re
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


def _submitted_energy(html: str) -> str:
    """The value the daily form will store for energy. The slider can sit at a
    midpoint for display; only the named field is what gets saved."""
    named = [tag for tag in re.findall(r"<input\b[^>]*>", html) if re.search(r'\bname="energy"', tag)]
    assert len(named) == 1, named
    match = re.search(r'\bvalue="([^"]*)"', named[0])
    assert match, named[0]
    return match.group(1)


def _energy_label(html: str) -> str:
    match = re.search(r'id="energy-val">([^<]*)<', html)
    assert match, "energy display missing"
    return match.group(1)


def _clear_day(day: str) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM daily_logs WHERE date = ?", (day,))
        conn.commit()
    finally:
        conn.close()


def _stored_energy(day: str):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT energy, notes FROM daily_logs WHERE date = ?", (day,)).fetchone()
    finally:
        conn.close()


def _save_daily(client, day: str, energy: str, notes: str):
    from conftest import csrf_token

    token = csrf_token(client, f"/daily/{day}")
    return client.post(
        "/daily/save",
        data={"date": day, "energy": energy, "notes": notes, "_csrf_token": token},
        follow_redirects=False,
    )


def test_saving_notes_without_rating_energy_keeps_it_empty(auth_client):
    """The slider used to sit at 5 and the route stored that, so notes on an
    unrated day wrote a fake energy and skewed the averages."""
    day = "2026-04-11"
    _clear_day(day)

    html = auth_client.get(f"/daily/{day}").text
    posted = _submitted_energy(html)
    assert posted == "", f"an unrated day must submit no energy, got {posted!r}"
    assert _energy_label(html) != "5", "an unrated day must not display a default rating"

    resp = _save_daily(auth_client, day, posted, "notes only")
    assert resp.status_code == 303, resp.text[:300]
    energy, notes = _stored_energy(day)
    assert energy is None
    assert notes == "notes only"

    again = auth_client.get(f"/daily/{day}").text
    assert _submitted_energy(again) == ""
    assert _energy_label(again) != "5"


def test_saving_notes_keeps_an_existing_energy_rating(auth_client):
    day = "2026-04-12"
    _clear_day(day)
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("INSERT INTO daily_logs (date, energy) VALUES (?, 8)", (day,))
        conn.commit()
    finally:
        conn.close()

    html = auth_client.get(f"/daily/{day}").text
    assert _submitted_energy(html) == "8"
    assert _energy_label(html) == "8"

    resp = _save_daily(auth_client, day, _submitted_energy(html), "still the same day")
    assert resp.status_code == 303, resp.text[:300]
    energy, notes = _stored_energy(day)
    assert (energy, notes) == (8, "still the same day")
