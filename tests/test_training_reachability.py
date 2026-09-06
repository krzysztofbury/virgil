"""No session may become unreachable, whatever its date."""

import json
import re
import sqlite3
from datetime import date

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
    target = 'data-training-date="2015-05-05"'
    html = auth_client.get("/training").text
    assert target not in html, "the oldest session cannot be in the current-year calendar"

    selected_year = 9999
    for _ in range(20):
        if target in html:
            break
        older_years = [int(value) for value in re.findall(r'href="/training\?year=(\d+)"', html)]
        older_years = [value for value in older_years if value < selected_year]
        assert older_years, "history calendar stopped offering an older recorded year"
        selected_year = max(older_years)
        html = auth_client.get(f"/training?year={selected_year}").text
    else:
        raise AssertionError("older sessions are not reachable through year navigation")
    assert marker

    assert target in html


def test_history_year_is_clamped_not_trusted(auth_client):
    """A hand-typed year must not reach calendar or SQLite unchecked."""
    resp = auth_client.get("/training?year=999999")
    assert resp.status_code == 200
    assert "<strong>9999</strong>" in resp.text
    below_range = auth_client.get("/training?year=0")
    assert below_range.status_code == 200
    assert "<strong>1</strong>" in below_range.text


def test_year_navigation_reaches_both_directions(auth_client):
    current_year = date.today().year
    early_year = current_year - 1
    late_year = current_year + 1
    _seed(f"{early_year}-12-31", notes="ZZ previous recorded year")
    _seed(f"{late_year}-01-01", notes="ZZ next recorded year")

    current = auth_client.get("/training").text
    assert f'href="/training?year={early_year}"' in current
    assert f'href="/training?year={late_year}"' in current

    previous = auth_client.get(f"/training?year={early_year}").text
    assert f'data-training-date="{early_year}-12-31"' in previous
    assert "ZZ previous recorded year" not in previous, "year pages must render summaries only"

    following = auth_client.get(f"/training?year={late_year}").text
    assert f'data-training-date="{late_year}-01-01"' in following
    assert "ZZ next recorded year" not in following, "year pages must render summaries only"


def test_pending_card_is_bounded(auth_client):
    """The pending list is bounded and says so, rather than growing forever."""
    from app.routers.training import MAX_PENDING_LISTED

    for day in range(1, MAX_PENDING_LISTED + 6):
        _seed(f"2017-{(day % 12) + 1:02d}-{(day % 28) + 1:02d}", parsed=PENDING, notes="ZZ pending flood")

    html = auth_client.get("/training").text
    # Count links inside the pending card only.
    card = html.split("Niedokończone", 1)[1].split("Workout History", 1)[0]
    listed = len(re.findall(r'href="/training/wod/confirm/\d+"', card))
    assert listed == MAX_PENDING_LISTED, f"pending card is unbounded: {listed} rows"
    assert "Niedokończonych sesji jest więcej" in card
