"""A logged experiment entry can be corrected from the page, within the rules logging uses."""

import sqlite3
from datetime import date, timedelta

import pytest

from tests.conftest import csrf_token, user_db_path

START = date.today() - timedelta(days=10)


@pytest.fixture
def experiment():
    """A completed four-week experiment with a yes/no and a minutes metric, removed afterwards."""
    conn = sqlite3.connect(user_db_path())
    exp_id = conn.execute(
        "INSERT INTO experiments (title, start_date, num_weeks, status) VALUES ('ZZ Correct me', ?, 4, 'completed')",
        (START.isoformat(),),
    ).lastrowid
    yes_no = conn.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, kind, color) VALUES (?, 'Cold shower', 'boolean', '#0af')",
        (exp_id,),
    ).lastrowid
    minutes = conn.execute(
        "INSERT INTO experiment_activity_types (experiment_id, name, kind, color) VALUES (?, 'Walk', 'duration', '#0f0')",
        (exp_id,),
    ).lastrowid
    entries = {}
    for name, metric, offset, value in (("yes1", yes_no, 1, 1), ("yes2", yes_no, 2, 0), ("walk", minutes, 1, 30)):
        entries[name] = conn.execute(
            "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, value, notes) VALUES (?, ?, ?, ?, '')",
            (exp_id, (START + timedelta(days=offset)).isoformat(), metric, value),
        ).lastrowid
    conn.commit()
    try:
        yield exp_id, entries
    finally:
        conn.execute("DELETE FROM experiment_entries WHERE experiment_id = ?", (exp_id,))
        conn.execute("DELETE FROM experiment_activity_types WHERE experiment_id = ?", (exp_id,))
        conn.execute("DELETE FROM experiments WHERE id = ?", (exp_id,))
        conn.commit()
        conn.close()


def _entry(entry_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT date, value, notes FROM experiment_entries WHERE id = ?", (entry_id,)).fetchone()
    finally:
        conn.close()


def _edit(client, exp_id, entry_id, **data):
    data["_csrf_token"] = csrf_token(client, f"/experiments/{exp_id}")
    return client.post(f"/experiments/{exp_id}/entry/{entry_id}/edit", data=data, follow_redirects=False)


def test_a_correction_rewrites_date_value_and_note(auth_client, experiment):
    exp_id, entries = experiment
    day = (START + timedelta(days=5)).isoformat()

    response = _edit(auth_client, exp_id, entries["walk"], date=day, value="45", notes="  hilly  ")

    assert _entry(entries["walk"]) == (day, 45, "hilly")
    assert response.headers["location"].startswith(f"/experiments/{exp_id}?msg=Entry+corrected")
    assert response.headers["location"].endswith("#entries")


@pytest.mark.parametrize(
    ("entry", "fields", "error"),
    [
        ("walk", {"date": (START - timedelta(days=1)).isoformat(), "value": "30"}, "Entry+date+must+be+between"),
        ("walk", {"date": START.isoformat(), "value": "0"}, "outside+the+allowed+range"),
        ("walk", {"date": START.isoformat(), "value": "2.5"}, "whole+number"),
        ("yes1", {"date": START.isoformat(), "value": "2"}, "outside+the+allowed+range"),
        ("yes1", {"date": (START + timedelta(days=2)).isoformat(), "value": "1"}, "already+has+an+answer"),
        ("walk", {"date": "someday", "value": "30"}, "valid+entry+date"),
    ],
)
def test_an_invalid_correction_changes_nothing(auth_client, experiment, entry, fields, error):
    exp_id, entries = experiment
    before = _entry(entries[entry])

    response = _edit(auth_client, exp_id, entries[entry], notes="", **fields)

    assert _entry(entries[entry]) == before
    assert error in response.headers["location"]


def test_an_entry_of_another_experiment_is_not_found(auth_client, experiment):
    exp_id, entries = experiment

    response = _edit(auth_client, exp_id + 1000, entries["walk"], date=START.isoformat(), value="30")

    assert "no+longer+exists" in response.headers["location"]
    assert _entry(entries["walk"])[1] == 30


def test_each_entry_offers_an_edit_form_and_a_confirmed_delete(auth_client, experiment):
    exp_id, entries = experiment

    html = auth_client.get(f"/experiments/{exp_id}").text

    assert f'action="/experiments/{exp_id}/entry/{entries["walk"]}/edit"' in html
    assert 'onsubmit="return confirm(this.dataset.confirm)"' in html
    assert "Delete the Walk entry from" in html
