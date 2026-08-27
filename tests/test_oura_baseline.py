"""Readiness against its own recent baseline, by rule and not by prose.

The page opened on six equal KPI cards and two charts, with nothing saying
whether today is different from the last week. By decision this states numbers
and one rule-derived word: no generated advice, no LLM call.
"""

from app.routers.oura import BASELINE_DAYS, BASELINE_TOLERANCE, readiness_baseline


def _rows(*scores):
    return [{"readiness_score": s} for s in scores]


def test_no_baseline_without_history():
    result = readiness_baseline([], 80)
    assert result["baseline"] is None
    assert result["status"] == ""
    assert result["days"] == 0


def test_no_baseline_without_today():
    result = readiness_baseline(_rows(70, 72, 74), None)
    assert result["today"] is None
    assert result["status"] == ""


def test_steady_inside_the_tolerance():
    result = readiness_baseline(_rows(70, 72, 71), 72)
    assert result["baseline"] == 71
    assert result["delta"] == 1
    assert result["status"] == "steady"


def test_above_and_below_the_tolerance():
    assert readiness_baseline(_rows(70, 70, 70), 70 + BASELINE_TOLERANCE)["status"] == "above"
    assert readiness_baseline(_rows(70, 70, 70), 70 - BASELINE_TOLERANCE)["status"] == "below"


def test_baseline_uses_at_most_the_window_and_skips_gaps():
    rows = _rows(*([80] * (BASELINE_DAYS + 5)))
    rows.insert(0, {"readiness_score": None})
    result = readiness_baseline(rows, 80)
    assert result["days"] == BASELINE_DAYS, "a missing day must not shrink or pad the window"
    assert result["baseline"] == 80


def test_page_orders_the_baseline_above_the_charts(auth_client):
    """Needs real rows: the vitals and the baseline only render with data."""
    import sqlite3
    from datetime import date, timedelta

    from conftest import user_db_path

    conn = sqlite3.connect(user_db_path())
    try:
        for offset in range(0, BASELINE_DAYS + 1):
            day = (date.today() - timedelta(days=offset)).isoformat()
            score = 88 if offset == 0 else 70
            conn.execute(
                "INSERT OR REPLACE INTO oura_daily (date, sleep_score, readiness_score, activity_score, steps, "
                "resting_hr, avg_hrv) VALUES (?, 80, ?, 75, 8000, 52.0, 45.0)",
                (day, score),
            )
        conn.commit()
    finally:
        conn.close()

    html = auth_client.get("/oura").text
    baseline = html.index('id="readiness-baseline"')
    for group in ("Sleep", "Activity", "Recovery"):
        assert f">{group}</h4>" in html, "the KPI cards must be grouped"
    assert baseline < html.index('class="vitals-group">Sleep'), "the baseline comes before the cards"
    assert "Compare metrics" in html, "the four-series chart is labelled as a comparison"
    assert html.index("Monthly Trends") < html.index("Compare metrics"), "the default trend comes first"
    assert baseline < html.index("Monthly Trends"), "the baseline comes before every chart"
    # 88 today against a 70 baseline is above it, by rule.
    assert "+18 versus baseline" in html
    assert "above" in html
