"""The dashboard's life scores section: the latest assessment first, with what
changed, and a form that adds to a date instead of overwriting it (#196)."""

import json
import re
import sqlite3

import pytest
from conftest import csrf_token, user_db_path

from app.db import LIFE_AREAS

LATEST = "2999-03-01"
PREVIOUS = "2999-01-01"


@pytest.fixture
def scores():
    conn = sqlite3.connect(user_db_path())
    saved = conn.execute("SELECT * FROM life_scores").fetchall()
    conn.execute("DELETE FROM life_scores")
    conn.commit()
    yield conn
    conn.execute("DELETE FROM life_scores")
    if saved:
        conn.executemany(f"INSERT INTO life_scores VALUES ({', '.join('?' * len(saved[0]))})", saved)
    conn.commit()
    conn.close()


def _assess(conn, day, power=None, diagnostic="", priorities="", **areas):
    values = {"date": day, "power_level": power, "diagnostic": diagnostic, "priorities": priorities, **areas}
    conn.execute(
        f"INSERT INTO life_scores ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
        tuple(values.values()),
    )
    conn.commit()


def _section(client) -> str:
    html = client.get("/").text
    return html[html.index('id="life-scores"') :]


def test_the_section_leads_with_the_latest_assessment_and_what_changed(auth_client, scores):
    _assess(scores, PREVIOUS, power=8.0, **dict.fromkeys(LIFE_AREAS, 6))
    _assess(
        scores,
        LATEST,
        power=7.0,
        diagnostic="Sleep slipped <again>.",
        priorities="One talk,\nthen rest.",
        **{**dict.fromkeys(LIFE_AREAS, 6), "health": 8, "family": 4},
    )
    section = _section(auth_client)
    head = section[section.index("life-latest") :]
    assert LATEST in head and "7.0" in head
    assert "-1.0" in head and f"since {PREVIOUS}" in head
    rows = re.findall(r'<li class="life-area">(.*?)</li>', section, re.S)
    assert len(rows) == len(LIFE_AREAS)
    by_label = {re.search(r"<span>(.*?)</span>", row).group(1): row for row in rows}
    assert "+2" in by_label["Health"] and "-2" in by_label["Family"]
    assert 'is-flat">=<' in by_label["Work"]
    assert "Sleep slipped &lt;again&gt;." in section, "the person's own text, escaped"
    assert "One talk,\nthen rest." in section


def test_a_first_assessment_shows_no_change(auth_client, scores):
    _assess(scores, LATEST, power=7.0, **dict.fromkeys(LIFE_AREAS, 6))
    section = _section(auth_client)
    assert "since" not in section[section.index("life-latest") : section.index("life-area-list")]


def test_a_missing_area_is_a_gap_not_a_zero(auth_client, scores):
    _assess(scores, LATEST, power=7.0, health=8)
    html = auth_client.get("/").text
    datasets = json.loads(re.search(r"createRadarChart\('radarChart', .*?, (\[.*?\])\);", html, re.S).group(1))
    assert datasets[0]["data"][LIFE_AREAS.index("health")] == 8
    assert datasets[0]["data"][LIFE_AREAS.index("family")] is None


def test_a_power_level_of_zero_is_shown(auth_client, scores):
    _assess(scores, LATEST, power=0.0)
    section = _section(auth_client)
    assert re.search(r'class="life-power">\s*0\.0\s*<', section)


def test_with_no_assessment_the_form_is_open(auth_client, scores):
    section = _section(auth_client)
    assert "life-latest" not in section
    assert re.search(r"<details[^>]* open>\s*<summary>New assessment", section)


def test_the_form_adds_to_a_date_and_leaves_older_metrics_alone(auth_client, scores):
    scores.execute(
        "INSERT INTO life_scores (date, health, weight, revenue, diagnostic) VALUES (?, 6, 80.5, 100, 'Kept')",
        (LATEST,),
    )
    scores.commit()
    response = auth_client.post(
        "/life-scores/save",
        data={"date": LATEST, "family": "4", "health": "", "diagnostic": "", "_csrf_token": csrf_token(auth_client, "/")},
        follow_redirects=False,
    )
    assert response.status_code == 303 and "#life-scores" in response.headers["location"]
    stored = scores.execute(
        "SELECT health, family, weight, revenue, diagnostic FROM life_scores WHERE date = ?", (LATEST,)
    ).fetchone()
    assert stored == (6, 4, 80.5, 100, "Kept")


def test_an_empty_form_is_refused(auth_client, scores):
    response = auth_client.post(
        "/life-scores/save",
        data={"date": LATEST, "_csrf_token": csrf_token(auth_client, "/")},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]
    assert scores.execute("SELECT COUNT(*) FROM life_scores").fetchone()[0] == 0
