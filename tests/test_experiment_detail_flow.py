"""One today question, three stats, secondary actions out of the way.

Quick-log at the top and the generic Log Entry form below asked for the same data
in two shapes, seven equal stats said nothing about whether the experiment was
working, and Delete sat at the same level as logging.
"""

import sqlite3
from datetime import date, timedelta

from conftest import user_db_path


def _experiment(kind="count", weeks=4, start_offset=-3):
    start = (date.today() + timedelta(days=start_offset)).isoformat()
    conn = sqlite3.connect(user_db_path())
    try:
        exp_id = conn.execute(
            "INSERT INTO experiments (title, description, start_date, num_weeks, status) "
            "VALUES ('ZZ Flow Probe', 'probe', ?, ?, 'active')",
            (start, weeks),
        ).lastrowid
        # target_value must be set: _metric_progress returns None without one, and
        # then the stats bar would carry two entries instead of the three under test.
        conn.execute(
            "INSERT INTO experiment_activity_types (experiment_id, name, kind, color, target_value, target_period) "
            "VALUES (?, 'Impuls', ?, '#22c55e', 8, 'total')",
            (exp_id, kind),
        )
        conn.commit()
        return exp_id
    finally:
        conn.close()


def test_today_question_comes_before_the_full_form(auth_client):
    exp_id = _experiment()
    html = auth_client.get(f"/experiments/{exp_id}").text
    quicklog = html.index("exp-quicklog")
    details = html.index("Different date or more detail")
    assert quicklog < details, "the one-tap question must come first"
    assert html.index("exp-stats-bar") < quicklog, "the outcome stays above the question"


def test_full_form_is_open_when_there_is_no_one_tap_path(auth_client):
    """A duration-only experiment has no quick-log, so the form must not hide."""
    exp_id = _experiment(kind="duration")
    html = auth_client.get(f"/experiments/{exp_id}").text
    start = html.index("Different date or more detail")
    tag_open = html.rindex("<details", 0, start)
    assert "open" in html[tag_open:start], "with no quick-log the full form must render open"


def test_stats_bar_shows_three_things(auth_client):
    exp_id = _experiment()
    html = auth_client.get(f"/experiments/{exp_id}").text
    bar = html[html.index("exp-stats-bar") : html.index("exp-quicklog")]
    assert bar.count("exp-stat-label") == 3, "outcome, target and time remaining - no more"
    assert "DAYS LEFT" in bar
    assert "Week 1 of 4" in html, "the week must be stated in words, not as 0% elapsed"


def test_destructive_actions_are_secondary(auth_client):
    exp_id = _experiment()
    html = auth_client.get(f"/experiments/{exp_id}").text
    delete = html.index(f"/experiments/{exp_id}/delete")
    summary = html.rindex("<summary>", 0, delete)
    assert "Actions" in html[summary : summary + 60], "Complete/Abandon/Delete belong behind a disclosure"
