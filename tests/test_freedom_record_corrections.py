"""Relapse records and day entries can be corrected or deleted from the Freedom page.

A slipped day writes its own relapse record, which follows the day: it is fixed
through the day. Any other record (older, or imported) is edited on its own.
"""

import sqlite3

import pytest

from tests.conftest import csrf_token, user_db_path


def _rows(sql, params=()):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


@pytest.fixture
def porn(auth_client):
    """Freedom on, the porn habit tracked, and its records for these tests cleared afterwards."""
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_freedom', '1')")
    conn.commit()
    conn.close()
    token = csrf_token(auth_client, "/freedom")
    auth_client.post("/freedom/habits", data={"slug": "porn", "_csrf_token": token}, follow_redirects=False)
    habit = _rows("SELECT id FROM habits WHERE slug = 'porn'")[0][0]
    conn = sqlite3.connect(user_db_path())
    imported = conn.execute(
        "INSERT INTO pmo_events (habit_id, date, event_type, notes) VALUES (?, '2025-02-01', 'relapse', 'imported')",
        (habit,),
    ).lastrowid
    conn.commit()
    conn.close()
    yield habit, imported
    conn = sqlite3.connect(user_db_path())
    conn.execute("DELETE FROM pmo_events WHERE habit_id = ? AND date LIKE '2025-0%'", (habit,))
    conn.execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date LIKE '2025-0%'", (habit,))
    conn.commit()
    conn.close()


def _post(client, path, **data):
    data["_csrf_token"] = csrf_token(client, "/freedom?habit=porn")
    return client.post(path, data=data, follow_redirects=False)


def _event(event_id):
    rows = _rows("SELECT date, event_type, notes FROM pmo_events WHERE id = ?", (event_id,))
    return rows[0] if rows else None


def test_an_imported_record_is_corrected(auth_client, porn):
    _, imported = porn

    response = _post(
        auth_client, f"/freedom/porn/relapses/{imported}/edit", date="2025-02-03", event_type="reset", notes=" moved "
    )

    assert _event(imported) == ("2025-02-03", "reset", "moved")
    assert response.headers["location"].startswith("/freedom?habit=porn&records=open&msg=Record+corrected")


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"date": "2999-01-01", "event_type": "relapse"}, "today+or+earlier"),
        ({"date": "2025-02-01", "event_type": "binge"}, "relapse+or+reset"),
        ({"date": "2025-02-01", "event_type": "relapse", "notes": "via daily log"}, "reserved"),
    ],
)
def test_an_invalid_correction_changes_nothing(auth_client, porn, fields, error):
    _, imported = porn

    response = _post(auth_client, f"/freedom/porn/relapses/{imported}/edit", **fields)

    assert _event(imported) == ("2025-02-01", "relapse", "imported")
    assert error in response.headers["location"]


def test_two_relapses_on_one_date_are_refused(auth_client, porn):
    habit, imported = porn
    conn = sqlite3.connect(user_db_path())
    other = conn.execute(
        "INSERT INTO pmo_events (habit_id, date, event_type, notes) VALUES (?, '2025-02-05', 'relapse', 'second')",
        (habit,),
    ).lastrowid
    conn.commit()
    conn.close()

    response = _post(auth_client, f"/freedom/porn/relapses/{other}/edit", date="2025-02-01", event_type="relapse")

    assert "already+a+relapse+on+2025-02-01" in response.headers["location"]
    assert _event(other)[0] == "2025-02-05"


def test_a_days_own_record_is_fixed_through_the_day(auth_client, porn):
    habit, _ = porn
    _post(auth_client, "/freedom/porn/daily", date="2025-03-10", slipped="1", amount="30")
    marker = _rows("SELECT id FROM pmo_events WHERE habit_id = ? AND date = '2025-03-10'", (habit,))[0][0]

    edit = _post(auth_client, f"/freedom/porn/relapses/{marker}/edit", date="2025-03-11", event_type="relapse")
    delete = _post(auth_client, f"/freedom/porn/relapses/{marker}/delete")
    html = auth_client.get("/freedom?habit=porn").text
    day_form = auth_client.get("/freedom?habit=porn&date=2025-03-10").text
    removed = _post(auth_client, "/freedom/porn/day/2025-03-10/delete")

    assert "correct+or+delete+the+day+instead" in edit.headers["location"]
    assert "correct+or+delete+the+day+instead" in delete.headers["location"]
    assert 'href="/freedom?habit=porn&amp;date=2025-03-10#day-card"' in html
    assert 'action="/freedom/porn/day/2025-03-10/delete"' in day_form, "the day form offers the delete"
    assert "Its relapse record goes with it." in day_form
    assert "day+2025-03-10+deleted" in removed.headers["location"]
    assert _rows("SELECT 1 FROM feniks_daily WHERE habit_id = ? AND date = '2025-03-10'", (habit,)) == []
    assert _event(marker) is None, "the day takes its own record with it"


def test_an_imported_record_is_deleted_and_another_habits_is_not_reachable(auth_client, porn):
    habit, imported = porn
    token = csrf_token(auth_client, "/freedom")
    auth_client.post("/freedom/habits", data={"slug": "alcohol", "_csrf_token": token}, follow_redirects=False)
    try:
        through_other = _post(auth_client, f"/freedom/alcohol/relapses/{imported}/delete")
        deleted = _post(auth_client, f"/freedom/porn/relapses/{imported}/delete")
    finally:
        _post(auth_client, "/freedom/alcohol/untrack")

    assert "no+longer+exists" in through_other.headers["location"]
    assert "Record+deleted" in deleted.headers["location"] and _event(imported) is None


def test_the_page_lists_records_and_offers_each_kind_its_own_correction(auth_client, porn):
    _, imported = porn

    html = auth_client.get("/freedom?habit=porn&records=open").text
    block = html[html.index('id="relapse-records"') :]

    assert 'id="relapse-records" open' in html
    assert f'action="/freedom/porn/relapses/{imported}/edit"' in block
    assert 'data-confirm="Delete the relapse record from 2025-02-01? The streak is counted again without it."' in block


def test_a_slipped_day_keeps_a_relapse_when_its_only_record_is_removed(auth_client, porn):
    """The imported record is that day's only relapse; deleting it hands the day its own marker."""
    habit, imported = porn
    _post(auth_client, "/freedom/porn/daily", date="2025-02-01", slipped="1", amount="30")
    assert _rows("SELECT notes FROM pmo_events WHERE habit_id = ? AND date = '2025-02-01'", (habit,)) == [("imported",)]

    _post(auth_client, f"/freedom/porn/relapses/{imported}/delete")

    assert _rows("SELECT notes FROM pmo_events WHERE habit_id = ? AND date = '2025-02-01'", (habit,)) == [
        ("via daily log",)
    ]


def test_a_relapse_cannot_be_moved_onto_a_day_logged_as_clean(auth_client, porn):
    _, imported = porn
    _post(auth_client, "/freedom/porn/daily", date="2025-02-02", slipped="0")

    response = _post(auth_client, f"/freedom/porn/relapses/{imported}/edit", date="2025-02-02", event_type="relapse")

    assert "logged+as+clean" in response.headers["location"]
    assert _event(imported)[0] == "2025-02-01"


@pytest.mark.parametrize("day", ["2999-01-01", "20250301", "yesterday"])
def test_a_day_delete_refuses_a_future_or_malformed_date(auth_client, porn, day):
    response = _post(auth_client, f"/freedom/porn/day/{day}/delete")

    assert "err=" in response.headers["location"]
