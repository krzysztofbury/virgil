"""Every history view offers the same 4, 12 and 26-week windows, chosen by ?weeks=."""

import re
import sqlite3
from datetime import date, timedelta

import pytest
from starlette.requests import Request

from app.db import LIFE_AREAS
from app.services.ranges import range_context, range_weeks
from tests.conftest import user_db_path

TODAY = date.today()
IN_FOUR = (TODAY - timedelta(days=3)).isoformat()
IN_TWELVE = (TODAY - timedelta(weeks=5)).isoformat()  # outside 4 weeks, inside 12
IN_TWENTY_SIX = (TODAY - timedelta(weeks=20)).isoformat()


@pytest.mark.parametrize(
    ("value", "weeks"), [("4", 4), ("12", 12), ("26", 26), ("7", 12), ("-4", 12), ("x", 12), (None, 12)]
)
def test_only_the_offered_windows_are_accepted(value, weeks):
    assert range_weeks(value) == weeks


def test_a_window_link_keeps_the_view_and_drops_the_moment():
    query = b"year=2025&day=2025-01-01&before_id=9&msg=Saved&job_id=3&weeks=4"
    request = Request({"type": "http", "path": "/training", "query_string": query, "headers": []})

    context = range_context(request, "personal-bests", drop=("before_id",))

    assert context["range_weeks"] == 4
    assert [(link["href"], link["active"]) for link in context["range_links"]] == [
        (f"/training?year=2025&day=2025-01-01&weeks={weeks}#personal-bests", weeks == 4) for weeks in (4, 12, 26)
    ]


@pytest.fixture
def user_rows():
    """Empty the given tables in the shared user database for one test, then put their rows back."""
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    saved = {}

    def clear(*tables):
        for table in tables:
            saved[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]  # noqa: S608
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
        conn.commit()
        return conn

    yield clear
    for table, rows in saved.items():
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
        for row in rows:
            conn.execute(
                f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
                tuple(row.values()),
            )
    conn.commit()
    conn.close()


def _history_dates(html: str) -> list[str]:
    history = html[html.index("<summary>History") :]
    history = history[: history.index("</details>")]
    return re.findall(r"<td>(\d{4}-\d{2}-\d{2})</td>", history)


def test_life_score_history_covers_the_window_including_incomplete_assessments(auth_client, user_rows):
    conn = user_rows("life_scores")
    complete = dict.fromkeys(LIFE_AREAS, 6)
    partial = {**complete, "family": None}
    for day, areas, power in ((IN_FOUR, complete, 7.0), (IN_TWELVE, partial, 5.0), (IN_TWENTY_SIX, complete, 3.0)):
        columns = ", ".join(["date", "power_level", *areas])
        conn.execute(
            f"INSERT INTO life_scores ({columns}) VALUES ({', '.join('?' * (len(areas) + 2))})",  # noqa: S608
            (day, power, *areas.values()),
        )
    conn.commit()

    four = auth_client.get("/?weeks=4").text
    twelve = auth_client.get("/?weeks=12").text
    twenty_six = auth_client.get("/?weeks=26").text

    assert _history_dates(four) == [IN_FOUR]
    assert _history_dates(twelve) == [IN_FOUR, IN_TWELVE], "newest first, and the incomplete one is listed"
    assert _history_dates(twenty_six) == [IN_FOUR, IN_TWELVE, IN_TWENTY_SIX]
    assert 'id="lifeHistoryChart"' not in four, "one assessment draws no line"
    assert "1 assessment in the last 4 weeks" in four
    assert 'id="lifeHistoryChart"' in twelve
    assert '"data": [5.0, 7.0], "label": "Power level"' in twelve, "oldest first on the chart"


def test_personal_bests_follow_the_window(auth_client, user_rows):
    conn = user_rows("training_entries", "training_sessions")
    exercise = conn.execute(
        "INSERT INTO training_exercises (name, section, metric, ad_hoc) VALUES ('ZZ window PB', 'Core', 'reps', 1)"
    ).lastrowid
    session = conn.execute("INSERT INTO training_sessions (date) VALUES (?)", (IN_TWELVE,)).lastrowid
    conn.execute(
        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) VALUES (?, ?, 1, 5, 60.0)",
        (session, exercise),
    )
    conn.commit()
    try:
        four = auth_client.get("/training?weeks=4").text
        twelve = auth_client.get("/training?weeks=12").text
    finally:
        conn.execute("DELETE FROM training_entries WHERE exercise_id = ?", (exercise,))
        conn.execute("DELETE FROM training_exercises WHERE id = ?", (exercise,))
        conn.commit()

    assert 'data-pb-exercise="ZZ window PB"' in twelve
    assert 'data-pb-exercise="ZZ window PB"' not in four
    assert "No loaded Core sets in the last 4 weeks." in four, "the card stays, so the picker can go back"


def test_oura_history_follows_the_window_but_the_baseline_does_not(auth_client, user_rows):
    conn = user_rows("oura_daily")
    days = [(TODAY, 88)] + [(TODAY - timedelta(days=n), 70) for n in range(1, 8)] + [(TODAY - timedelta(weeks=5), 50)]
    for day, score in days:
        conn.execute("INSERT INTO oura_daily (date, readiness_score) VALUES (?, ?)", (day.isoformat(), score))
    conn.commit()

    four = auth_client.get("/oura?weeks=4").text
    twelve = auth_client.get("/oura?weeks=12").text

    old_row = f"<td><small>{IN_TWELVE}</small></td>"
    assert old_row in twelve and old_row not in four
    assert "+18 versus baseline" in four and "+18 versus baseline" in twelve
