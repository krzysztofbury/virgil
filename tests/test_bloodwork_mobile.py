"""The mobile Blood Work list shows the value, the status and the direction.

The full matrix is Marker, Unit, Ref, then one column per test date. On a phone
those three metadata columns fill the viewport, so every value and the chart
action sat off-screen inside a scroll container with no visible affordance.
"""

import sqlite3

from conftest import user_db_path

from app.routers.bloodwork import marker_summary


def test_summary_is_empty_without_results():
    assert marker_summary({}) == {"date": "", "value": "", "flag": "", "previous": "", "change": ""}


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
    assert summary["previous"] == "5"
    assert summary["change"] == "+0.4"


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

    html = auth_client.get("/bloodwork").text
    assert 'class="bw-list on-mobile"' in html, "no mobile result list rendered"
    assert "ZZ Ferritin" in html
    assert "55.5" in html
    assert "+13.5" in html, "the change from the previous result must be shown"
    assert "on-desktop" in html, "the full matrix must stay, switched off on a phone"
    assert "All results" in html, "the matrix must stay reachable on a phone"
    # One macro, two call sites: the table markup must not be copied.
    from pathlib import Path

    template = Path("app/templates/bloodwork.html").read_text(encoding="utf-8")
    assert template.count("<thead>") == 1, "the matrix markup is duplicated instead of shared"
    assert template.count("results_matrix(") == 3, "expected one macro definition and two call sites"
