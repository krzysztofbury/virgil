"""The dashboard's first viewport must carry orientation and the next action.

The audit measured about 3413 px of equally weighted blocks on a phone, with the
week strip first and nothing actionable above the fold.
"""

import sqlite3

from conftest import user_db_path


def _positions(html: str, *markers: str) -> list[int]:
    found = []
    for marker in markers:
        index = html.find(marker)
        assert index != -1, f"missing from the dashboard: {marker!r}"
        found.append(index)
    return found


def test_today_block_comes_before_the_analytics(auth_client):
    html = auth_client.get("/").text
    # Section titles, not bare words: the nav carries an "Oura" link of its own.
    today, week, insights, oura, calendar = _positions(
        html,
        'id="today-block"',
        '<div class="section-title">Week</div>',
        '<div class="section-title">Insights</div>',
        '<div class="section-title">Oura</div>',
        'id="year-calendar"',
    )
    assert today < week, "the next action must sit above the week strip"
    assert week < insights < oura, "Oura belongs under Insights"
    assert insights < calendar, "the year calendar is the last thing on the page"


def test_today_block_carries_the_cta_and_the_count(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        # INSERT OR IGNORE then UPDATE: the user DB is session-scoped, and OR
        # REPLACE would wipe columns another test wrote on today's row.
        conn.execute("INSERT OR IGNORE INTO daily_logs (date) VALUES (date('now'))")
        conn.execute(
            "UPDATE daily_logs SET energy = 7, andy_body_status = 'done', andy_spirit_status = 'done', "
            "andy_account_status = 'pending', andy_relations_status = 'skipped' WHERE date = date('now')"
        )
        conn.commit()
    finally:
        conn.close()

    html = auth_client.get("/").text
    assert "2/4" in html, "the A.N.D.Y. count must be stated, not counted by eye"
    assert "Finish today" in html or "Edit today" in html, "the CTA must name the next action"


def test_year_calendar_is_collapsed_and_holds_no_canvas(auth_client):
    """A canvas inside a closed details renders at zero size and stays wrong."""
    html = auth_client.get("/").text
    start = html.index('id="year-calendar"')
    block = html[start : html.index("</details>", start)]
    assert "<canvas" not in block, "a chart must never be collapsed"
    assert "year-cal-grid" in block
