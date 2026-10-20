"""One set of a stored workout can be corrected or deleted from the day dialog."""

import sqlite3

import pytest

from tests.conftest import csrf_token, user_db_path

DAY = "2025-03-04"


@pytest.fixture
def workout():
    """A session with two sets of a library movement, removed afterwards."""
    conn = sqlite3.connect(user_db_path())
    library = conn.execute(
        "INSERT INTO exercise_library (name, section, metric) VALUES ('ZZ Goblet squat', 'Core', 'reps')"
    ).lastrowid
    other = conn.execute(
        "INSERT INTO exercise_library (name, section, metric) VALUES ('ZZ Plank', 'Core', 'time')"
    ).lastrowid
    exercise = conn.execute(
        "INSERT INTO training_exercises (name, section, metric) VALUES ('ZZ Goblet squat', 'Core', 'reps')"
    ).lastrowid
    session = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (DAY,)).lastrowid
    sets = [
        conn.execute(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, ?, 10, 16)",
            (session, exercise, n),
        ).lastrowid
        for n in (1, 2)
    ]
    conn.commit()
    try:
        yield session, sets
    finally:
        conn.execute("DELETE FROM training_entries WHERE session_id = ?", (session,))
        conn.execute("DELETE FROM training_sessions WHERE id = ?", (session,))
        conn.execute("DELETE FROM training_exercises WHERE name IN ('ZZ Goblet squat', 'ZZ Plank')")
        conn.execute("DELETE FROM exercise_library WHERE id IN (?, ?)", (library, other))
        conn.commit()
        conn.close()


def _set(entry_id):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute(
            "SELECT tex.name, te.set_number, te.reps, te.weight, te.duration, te.notes, te.equipment "
            "FROM training_entries te JOIN training_exercises tex ON tex.id = te.exercise_id WHERE te.id = ?",
            (entry_id,),
        ).fetchone()
    finally:
        conn.close()


def _post(client, path, **data):
    data["_csrf_token"] = csrf_token(client, "/training")
    return client.post(path, data=data, follow_redirects=False)


def test_a_set_is_corrected_and_the_day_dialog_reopens(auth_client, workout):
    _, (first, _) = workout

    response = _post(
        auth_client,
        f"/training/entry/{first}/edit",
        movement="ZZ Plank",
        set_number="1",
        reps="",
        weight="",
        duration_minutes="1.5",
        equipment="",
        note=" held  ",
    )

    assert _set(first) == ("ZZ Plank", 1, None, None, 90.0, "held", None)
    assert response.headers["location"].startswith(f"/training?year=2025&day={DAY}&msg=Set+corrected")
    assert response.headers["location"].endswith(f"#training-day-{DAY}")


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"movement": "ZZ Not a movement", "reps": "10"}, "not+in+the+exercise+library"),
        ({"movement": "ZZ Goblet squat", "reps": "1001"}, "reps+must+be+between+0+and+1000"),
        ({"movement": "ZZ Goblet squat", "weight": "heavy"}, "weight+must+be+a+number"),
        ({"movement": "ZZ Goblet squat", "duration_minutes": "inf"}, "time+must+be+a+number"),
        ({"movement": "ZZ Goblet squat", "equipment": "spaceship"}, "equipment+must+be+one+of"),
        ({"movement": "", "reps": "10"}, "movement+is+required"),
        ({"movement": "ZZ Goblet squat", "set_number": "0"}, "set_number+must+be+between+1+and+100"),
        ({"movement": "ZZ Goblet squat", "duration_minutes": "-1"}, "time+must+be+between+0+and+1440+minutes"),
    ],
)
def test_an_invalid_correction_changes_nothing(auth_client, workout, fields, error):
    _, (first, _) = workout
    before = _set(first)

    response = _post(auth_client, f"/training/entry/{first}/edit", **fields)

    assert _set(first) == before
    assert "Set+not+saved" in response.headers["location"] and error in response.headers["location"]


def test_deleting_a_set_keeps_the_rest_of_the_workout(auth_client, workout):
    session, (first, second) = workout

    _post(auth_client, f"/training/entry/{first}/delete")
    again = _post(auth_client, f"/training/entry/{first}/delete")

    assert _set(first) is None and _set(second) is not None
    assert "no+longer+exists" in again.headers["location"]


def test_the_day_dialog_offers_each_set_for_correction(auth_client, workout):
    _, (first, second) = workout

    html = auth_client.get(f"/training?year=2025&day={DAY}").text
    dialog = html[html.index(f'id="training-day-{DAY}"') :]

    assert f'action="/training/entry/{first}/edit"' in dialog and f'action="/training/entry/{second}/delete"' in dialog
    assert '<option value="ZZ Goblet squat">' in dialog, "the movement field suggests library names"
    assert 'data-confirm="Delete ZZ Goblet squat set 2? The rest of the workout stays."' in dialog


def test_saving_an_unchanged_time_keeps_its_exact_seconds(auth_client, workout):
    """37 s shows as 0.62 min; posting that back must not store 37.2 s."""
    _, (first, _) = workout
    conn = sqlite3.connect(user_db_path())
    conn.execute("UPDATE training_entries SET duration = 37 WHERE id = ?", (first,))
    conn.commit()
    conn.close()

    html = auth_client.get(f"/training?year=2025&day={DAY}").text
    shown = html[html.index(f'action="/training/entry/{first}/edit"') :]
    shown = shown[shown.index('name="duration_minutes" value="') + len('name="duration_minutes" value="') :]
    shown = shown[: shown.index('"')]
    _post(
        auth_client,
        f"/training/entry/{first}/edit",
        movement="ZZ Goblet squat",
        set_number="1",
        reps="10",
        weight="16",
        duration_minutes=shown,
        note="only the note changed",
    )

    assert shown == "0.62"
    assert _set(first)[4] == 37.0 and _set(first)[5] == "only the note changed"


def test_deleting_the_last_set_keeps_the_session(auth_client, workout):
    session, sets = workout

    for entry in sets:
        _post(auth_client, f"/training/entry/{entry}/delete")

    conn = sqlite3.connect(user_db_path())
    try:
        assert conn.execute("SELECT 1 FROM training_sessions WHERE id = ?", (session,)).fetchone() == (1,)
    finally:
        conn.close()
