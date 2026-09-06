"""No session may become unreachable, whatever its date."""

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


def test_history_year_navigation_reaches_older_sessions(auth_client):
    marker = _seed("2015-05-05", notes="ZZ page probe marker")
    html = auth_client.get("/training").text
    assert "ZZ page probe marker" not in html, "the oldest session cannot be in the current-year calendar"

    selected_year = 9999
    for _ in range(20):
        if "ZZ page probe marker" in html:
            break
        older_years = [int(value) for value in re.findall(r'href="/training\?year=(\d+)"', html)]
        older_years = [value for value in older_years if value < selected_year]
        assert older_years, "history calendar stopped offering an older recorded year"
        selected_year = max(older_years)
        html = auth_client.get(f"/training?year={selected_year}").text
    else:
        raise AssertionError("older sessions are not reachable through year navigation")
    assert marker

    assert 'data-training-date="2015-05-05"' in html


def test_history_year_is_clamped_not_trusted(auth_client):
    """A hand-typed year must not reach calendar or SQLite unchecked."""
    resp = auth_client.get("/training?year=999999")
    assert resp.status_code == 200
    assert "<strong>9999</strong>" in resp.text
    below_range = auth_client.get("/training?year=0")
    assert below_range.status_code == 200
    assert "<strong>1</strong>" in below_range.text


def test_every_valid_iso_year_is_reachable(auth_client):
    early = _seed("1899-12-31", notes="ZZ early ISO year")
    late = _seed("2101-01-01", notes="ZZ late ISO year")

    assert "ZZ early ISO year" in auth_client.get("/training?year=1899").text
    assert "ZZ late ISO year" in auth_client.get("/training?year=2101").text
    assert early and late


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
