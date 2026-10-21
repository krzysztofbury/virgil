"""The mobile Blood Work list shows the value, the status and the direction.

The full matrix is Marker, Unit, Ref, then one column per test date. On a phone
those three metadata columns fill the viewport, so every value and the chart
action sat off-screen inside a scroll container with no visible affordance.
"""

import sqlite3

from conftest import user_db_path

from app.routers.bloodwork import marker_summary


def test_summary_is_empty_without_results():
    assert marker_summary({}) == {
        "date": "",
        "value": "",
        "flag": "",
        "previous": "",
        "change": "",
        "direction": "",
        "trend": "",
    }


def test_summary_takes_the_latest_date_and_the_signed_change():
    summary = marker_summary(
        {
            "2026-01-10": {"value": 5.0, "value_text": "", "flag": ""},
            "2026-06-10": {"value": 5.4, "value_text": "", "flag": "H"},
        }
    )
    assert summary["date"] == "2026-06-10"
    assert summary["value"] == "5.4"
    assert summary["flag"] == "H"
    assert summary["previous"] == "5.0", "a value keeps the precision the marker is reported in"
    assert summary["change"] == "+0.4"
    assert summary["direction"] == "up"


def test_precision_follows_the_reference_range():
    """HbA1c 5.0 against a 5.6 limit printed as "5" hid how close it is."""
    summary = marker_summary(
        {"2026-06-10": {"value": 5.0, "value_text": "", "flag": ""}}, {"ref_low": 4.0, "ref_high": 5.6}
    )
    assert summary["value"] == "5.0"
    whole = marker_summary(
        {
            "2026-01-10": {"value": 330.0, "value_text": "", "flag": ""},
            "2026-06-10": {"value": 900.0, "value_text": "", "flag": ""},
        },
        {"ref_low": 300.0, "ref_high": 1000.0},
    )
    assert (whole["previous"], whole["value"], whole["change"]) == ("330", "900", "+570")


def _trend(before, after, low=None, high=None):
    return marker_summary(
        {
            "2026-01-10": {"value": before, "value_text": "", "flag": ""},
            "2026-06-10": {"value": after, "value_text": "", "flag": ""},
        },
        {"ref_low": low, "ref_high": high},
    )["trend"]


def test_the_trend_says_whether_a_move_is_good_news_for_that_marker():
    assert _trend(121, 142, 0, 100) == "worse", "LDL rising further above its range"
    assert _trend(142, 121, 0, 100) == "better", "LDL falling back towards its range"
    assert _trend(22, 41, 30, 100) == "better", "vitamin D rising into its range"
    assert _trend(35, 25, 30, 100) == "worse", "vitamin D falling out of its range"
    assert _trend(96, 88, 30, 400) == "", "moving about inside the range is not news"
    assert _trend(96, 88) == "", "no range, no judgement"
    assert _trend(50, 50, 30, 100) == ""


def test_summary_has_no_change_for_a_single_result():
    summary = marker_summary({"2026-06-10": {"value": 5.4, "value_text": "", "flag": ""}})
    assert summary["value"] == "5.4"
    assert summary["previous"] == ""
    assert summary["change"] == ""


def test_summary_drops_the_change_for_a_text_result():
    """A text result ("negative", "traces") has no direction to report.

    blood_results.value is REAL NOT NULL, so a text result still carries a
    number (usually 0). value_text is the only honest signal that the number is
    not the result, so the guard reads it - `value is None` never happens here.
    """
    summary = marker_summary(
        {
            "2026-01-10": {"value": 0.0, "value_text": "negative", "flag": ""},
            "2026-06-10": {"value": 0.0, "value_text": "positive", "flag": "H"},
        }
    )
    assert summary["value"] == "positive"
    assert summary["change"] == ""


def test_bloodwork_page_renders_a_mobile_list(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        marker_id = conn.execute(
            "INSERT INTO blood_markers (category, name, unit, ref_low, ref_high, display_order) "
            "VALUES ('ZZ Mobile', 'ZZ Ferritin', 'ng/ml', 30, 400, 900)"
        ).lastrowid
        conn.execute(
            "INSERT INTO blood_results (marker_id, date, value, flag) VALUES (?, '2026-02-01', 42.0, '')",
            (marker_id,),
        )
        conn.execute(
            "INSERT INTO blood_results (marker_id, date, value, flag) VALUES (?, '2026-07-01', 55.5, '')",
            (marker_id,),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        html = auth_client.get("/bloodwork").text
    finally:
        # Shared database: another module's fixture adds a marker of this name.
        conn = sqlite3.connect(user_db_path())
        conn.execute("DELETE FROM blood_results WHERE marker_id = ?", (marker_id,))
        conn.execute("DELETE FROM blood_markers WHERE id = ?", (marker_id,))
        conn.commit()
        conn.close()
    assert 'class="bw-list on-mobile"' in html, "no mobile result list rendered"
    assert "ZZ Ferritin" in html
    assert "55.5" in html
    assert "from 42.0" in html and "+13.5" in html, "the change and what it is from must be shown"
    assert "bw-trend-neutral" in html, "a move inside the range is neither good nor bad"
    assert "on-desktop" in html, "the full matrix must stay, switched off on a phone"
    assert "All results" in html, "the matrix must stay reachable on a phone"
    # One macro, two call sites: the table markup must not be copied.
    from pathlib import Path

    template = Path("app/templates/bloodwork.html").read_text(encoding="utf-8")
    assert template.count("<thead>") == 1, "the matrix markup is duplicated instead of shared"
    assert template.count("results_matrix(") == 3, "expected one macro definition and two call sites"
