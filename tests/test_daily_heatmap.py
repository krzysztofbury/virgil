"""Daily trends show 12 weeks of completion, as a table that is its own text equivalent."""

import asyncio
import re
import sqlite3
from datetime import date

import aiosqlite
import pytest

from app.migrations.runner import run_migrations
from app.services.daily_log import STATUS_FIELDS, completion_weeks
from tests.conftest import user_db_path

LAST = date(2026, 10, 21)  # a Wednesday
ALL_DONE = dict.fromkeys(STATUS_FIELDS, "done")
LOGS = {
    "2026-08-02": ALL_DONE,  # the Sunday before the first week: outside the range
    "2026-08-04": {"water": "done", "andy_body_status": "done"},
    "2026-10-19": ALL_DONE,
    "2026-10-20": {},  # logged, nothing done
    "2026-10-22": ALL_DONE,  # after the last day: outside the range
}


def _insert(day: str, statuses: dict) -> tuple[str, tuple]:
    columns = ", ".join(["date", *statuses])
    placeholders = ", ".join("?" * (len(statuses) + 1))
    return f"INSERT INTO daily_logs ({columns}) VALUES ({placeholders})", (day, *statuses.values())  # noqa: S608


def test_twelve_monday_weeks_end_with_the_week_of_the_last_day(tmp_path):
    async def scenario():
        db = await aiosqlite.connect(tmp_path / "heatmap.db")
        db.row_factory = aiosqlite.Row
        try:
            await run_migrations(db)
            for day, statuses in LOGS.items():
                await db.execute(*_insert(day, statuses))
            await db.commit()
            return await completion_weeks(db, LAST, 12)
        finally:
            await db.close()

    weeks = asyncio.run(scenario())

    assert [week["start"] for week in (weeks[0], weeks[-1])] == ["2026-08-03", "2026-10-19"]
    assert all(date.fromisoformat(week["start"]).weekday() == 0 for week in weeks)
    assert len(weeks) == 12 and all(len(week["days"]) == 7 for week in weeks)

    first, last = weeks[0], weeks[-1]
    assert [day["done"] for day in first["days"][:3]] == [None, 2, None], "unlogged stays distinct from 0"
    assert first["average"] == round(2 / 49 * 100)
    assert first["month"] == "Aug"

    assert [(day["done"], day["future"]) for day in last["days"][:4]] == [
        (7, False),
        (0, False),
        (None, False),
        (None, True),
    ], "the day after the last one is future even when it has a log"
    assert last["average"] == round(7 / 21 * 100), "the average covers only the days up to the last one"
    assert [week["month"] for week in weeks].count("") == 12 - 3, "one label per month: Aug, Sep, Oct"


@pytest.fixture
def only_one_logged_day():
    """The session's user database is shared: clear the range, then put back what was there."""
    conn = sqlite3.connect(user_db_path())
    conn.row_factory = sqlite3.Row
    span = ("2026-08-03", "2026-10-25")
    saved = [dict(row) for row in conn.execute("SELECT * FROM daily_logs WHERE date BETWEEN ? AND ?", span)]
    conn.execute("DELETE FROM daily_logs WHERE date BETWEEN ? AND ?", span)
    conn.execute(*_insert("2026-10-19", ALL_DONE))
    conn.commit()
    try:
        yield
    finally:
        conn.execute("DELETE FROM daily_logs WHERE date BETWEEN ? AND ?", span)
        for row in saved:
            conn.execute(*_insert(row.pop("date"), row))
        conn.commit()
        conn.close()


def test_the_daily_page_renders_the_weeks_as_a_labelled_table(auth_client, only_one_logged_day):
    html = auth_client.get(f"/daily/{LAST.isoformat()}").text
    table = html[html.index('class="heatmap-weeks"') : html.index("</table>")]

    assert "<caption" in table and "12 weeks up to 2026-10-21" in table
    assert len(re.findall(r'<td class="heatmap-day', table)) == 84
    assert "Week of 2026-10-19" in table
    assert '<span class="sr-only">7 of 7</span>' in table, "a screen reader reads each day as a count"
    assert '<span class="sr-only">Not logged</span>' in table
    assert table.count('<span class="sr-only">After this day</span>') == 4, "Thursday to Sunday of the last week"
    averages = re.findall(r"<td>(\d+)</td>", table[table.index("<tfoot>") :])
    assert averages == ["0"] * 11 + [str(round(7 / 21 * 100))]


def test_the_seven_day_heatmap_reads_from_the_same_weeks(auth_client, only_one_logged_day):
    html = auth_client.get(f"/daily/{LAST.isoformat()}").text

    week = re.findall(r'class="heatmap-cell heatmap-cell--week" title="([^"]+)"', html)
    assert week == [f"2026-10-{day}: {pct}%" for day, pct in zip(range(15, 22), [0, 0, 0, 0, 100, 0, 0], strict=True)]
