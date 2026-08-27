"""No session may become unreachable, whatever its date.

/training read the newest 20 sessions by date. Backdating a capture hid it and
its "dokończ" link at once, while the confirm screen told the user the session
was visible there.
"""

import json
import re
import sqlite3

from conftest import user_db_path

PENDING = json.dumps({"entries": [], "unmatched": [], "parse_error": ""})


def _seed(date_str, parsed=None, notes="ZZ reach probe"):
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, notes, wod_parsed) VALUES (?, ?, ?)",
            (date_str, notes, parsed),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_backdated_pending_session_is_listed(auth_client):
    """25 newer sessions push this one far outside the newest-20 history window."""
    for day in range(1, 26):
        _seed(f"2026-06-{day:02d}")
    old = _seed("2019-01-01", parsed=PENDING, notes="ZZ ancient pending")

    html = auth_client.get("/training").text
    assert f"/training/wod/confirm/{old}" in html, "a backdated pending session vanished with its only route back"
    assert "Niedokończone" in html


def test_history_pages_reach_older_sessions(auth_client):
    marker = _seed("2015-05-05", notes="ZZ page probe marker")
    first = auth_client.get("/training").text
    assert "ZZ page probe marker" not in first, "the oldest session cannot be on page 1 here"

    for page in range(2, 13):
        html = auth_client.get(f"/training?page={page}").text
        if "ZZ page probe marker" in html:
            break
    else:
        raise AssertionError("older sessions are not reachable through pagination")
    assert marker

    assert "starsze" in first, "page 1 must offer a way to older sessions"
    assert "nowsze" in auth_client.get("/training?page=2").text


def test_page_number_is_clamped_not_trusted(auth_client):
    """A hand-typed page number must not become an unbounded OFFSET."""
    resp = auth_client.get("/training?page=999999")
    assert resp.status_code == 200
    assert auth_client.get("/training?page=0").status_code == 200
    assert auth_client.get("/training?page=-5").status_code == 200


def test_pending_card_is_bounded(auth_client):
    """The pending list is bounded and says so, rather than growing forever."""
    from app.routers.training import MAX_PENDING_LISTED

    for day in range(1, MAX_PENDING_LISTED + 6):
        _seed(f"2017-{(day % 12) + 1:02d}-{(day % 28) + 1:02d}", parsed=PENDING, notes="ZZ pending flood")

    html = auth_client.get("/training").text
    # Count links inside the pending card only. The history section below renders
    # its own "dokończ" link per pending session, which is a different list.
    card = html.split("Niedokończone", 1)[1].split("Workout History", 1)[0]
    listed = len(re.findall(r'href="/training/wod/confirm/\d+"', card))
    assert listed == MAX_PENDING_LISTED, f"pending card is unbounded: {listed} rows"
    assert "Niedokończonych sesji jest więcej" in card
