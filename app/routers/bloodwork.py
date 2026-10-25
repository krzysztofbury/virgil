import sqlite3
from math import isfinite
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.feedback import error_redirect, success_redirect
from app.i18n import _
from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormFloat, truncate, valid_date

router = APIRouter()


# More than any lab reports, and short of float noise (5.0400000001).
DECIMALS_MAX = 3


def _decimals(value) -> int:
    text = f"{float(value):.{DECIMALS_MAX}f}".rstrip("0")
    return len(text.split(".")[1]) if "." in text else 0


def marker_decimals(marker: dict, marker_results: dict) -> int:
    """How many decimals this marker is reported in: the most that its reference
    limits or any of its results carry. HbA1c with a 5.6 limit prints 5.0, not
    5, which is the difference between in range and on its edge."""
    values = [marker.get("ref_low"), marker.get("ref_high")]
    values += [r.get("value") for r in marker_results.values() if not r.get("value_text")]
    return max((_decimals(v) for v in values if v is not None), default=0)


def _as_display(row, decimals: int) -> str:
    """A result as the page prints it: the text form wins over the number."""
    if row.get("value_text"):
        return str(row["value_text"])
    value = row.get("value")
    if value is None:
        return ""
    return f"{value:.{decimals}f}"


def _trend(previous: float, latest: float, low, high) -> str:
    """Whether a move is good news for this marker, judged by its range.

    Away from the range, or leaving it, is worse; back towards it, or into it,
    is better. Moving about inside the range, or with no range, says nothing.
    """
    if latest == previous:
        return ""
    rising = latest > previous
    if high is not None and latest > high:
        return "worse" if rising else "better"
    if low is not None and latest < low:
        return "better" if rising else "worse"
    outside_before = (high is not None and previous > high) or (low is not None and previous < low)
    return "better" if outside_before else ""


def marker_summary(marker_results: dict, marker: dict | None = None) -> dict:
    """Latest value, its flag, and the change from the result before it.

    The mobile list answers one question - "where is this marker now, and which
    way did it move" - which the full date matrix can only answer by scrolling
    sideways past three columns of metadata.

    `change` stays empty unless both results are numeric. blood_results.value is
    REAL NOT NULL, so a text result ("negative", "traces") still stores a number;
    value_text is what says the number is not the result, and subtracting those
    numbers would report a direction the user never recorded.
    """
    empty = {"date": "", "value": "", "flag": "", "previous": "", "change": "", "direction": "", "trend": ""}
    if not marker_results:
        return empty
    marker = marker or {}
    decimals = marker_decimals(marker, marker_results)

    dates = sorted(marker_results)
    latest = marker_results[dates[-1]]
    previous = marker_results[dates[-2]] if len(dates) > 1 else None

    summary = {
        **empty,
        "date": dates[-1],
        "value": _as_display(latest, decimals),
        "flag": latest.get("flag") or "",
        "previous": _as_display(previous, decimals) if previous else "",
    }
    numeric = (
        previous is not None
        and not latest.get("value_text")
        and not previous.get("value_text")
        and latest.get("value") is not None
        and previous.get("value") is not None
    )
    if numeric:
        before, now = float(previous["value"]), float(latest["value"])
        summary["change"] = f"{now - before:+.{decimals}f}"
        summary["direction"] = "up" if now > before else "down" if now < before else "flat"
        summary["trend"] = _trend(before, now, marker.get("ref_low"), marker.get("ref_high"))
    return summary


@router.get("/bloodwork", response_class=HTMLResponse)
@router.get("/bloodwork/{category}", response_class=HTMLResponse)
async def bloodwork_page(request: Request, category: str = "all"):
    db = get_user_db_from_request(request)

    # Get all categories
    cats = await db.execute_fetchall("SELECT DISTINCT category FROM blood_markers ORDER BY display_order, category")
    categories = [r["category"] for r in cats]

    # Get markers (filtered or all)
    if category != "all":
        markers = await db.execute_fetchall(
            "SELECT * FROM blood_markers WHERE category = ? ORDER BY display_order, name", (category,)
        )
    else:
        markers = await db.execute_fetchall("SELECT * FROM blood_markers ORDER BY display_order, name")
    markers = [dict(m) for m in markers]

    # Get all test dates
    dates_rows = await db.execute_fetchall("SELECT DISTINCT date FROM blood_results ORDER BY date")
    test_dates = [r["date"] for r in dates_rows]

    # Get results keyed by (marker_id, date) — single query
    marker_ids = [m["id"] for m in markers]
    results: dict[int, dict[str, dict]] = {mid: {} for mid in marker_ids}
    if marker_ids:
        placeholders = ",".join("?" * len(marker_ids))
        all_results = await db.execute_fetchall(
            f"SELECT * FROM blood_results WHERE marker_id IN ({placeholders}) ORDER BY date",
            marker_ids,
        )
        for r in all_results:
            results[r["marker_id"]][r["date"]] = dict(r)

    for m in markers:
        marker_results = results.get(m["id"], {})
        decimals = marker_decimals(m, marker_results)
        for r in marker_results.values():
            r["display"] = _as_display(r, decimals)
        m["summary"] = marker_summary(marker_results, m)

    # Selected marker for chart (first one or from query param)
    chart_marker_id = request.query_params.get("marker")
    chart_marker = None
    chart_labels = []
    chart_values = []
    chart_ref_low = None
    chart_ref_high = None
    chart_results = []
    if chart_marker_id and chart_marker_id.isdigit():
        mid = int(chart_marker_id)
        m_row = await db.execute_fetchall("SELECT * FROM blood_markers WHERE id = ?", (mid,))
        if m_row:
            chart_marker = dict(m_row[0])
            chart_ref_low = chart_marker.get("ref_low")
            chart_ref_high = chart_marker.get("ref_high")
            # Its own query: the marker can sit outside the category tab shown.
            chart_results = [
                dict(r)
                for r in await db.execute_fetchall(
                    "SELECT * FROM blood_results WHERE marker_id = ? ORDER BY date DESC", (mid,)
                )
            ]
            for r in reversed(chart_results):
                chart_labels.append(r["date"])
                chart_values.append(r["value"])

    return templates.TemplateResponse(
        request,
        "bloodwork.html",
        {
            "request": request,
            "categories": categories,
            "current_category": category,
            "markers": markers,
            "test_dates": test_dates,
            "results": results,
            "chart_marker": chart_marker,
            # Newest first, for correcting or deleting one result at a time. A flag
            # that matches the range is offered as "from the range", so a corrected
            # value does not keep a flag its old value earned.
            "chart_results": [
                {
                    **r,
                    "flag_is_auto": r["flag"]
                    == ("" if r["value_text"] else compute_flag(r["value"], chart_ref_low, chart_ref_high)),
                }
                for r in chart_results
            ],
            "chart_labels": chart_labels,
            "chart_values": chart_values,
            "chart_ref_low": chart_ref_low,
            "chart_ref_high": chart_ref_high,
        },
    )


def compute_flag(value: float, ref_low: float | None, ref_high: float | None) -> str:
    """Derive the out-of-range flag from the marker's reference bounds."""
    if ref_low is not None and value < ref_low:
        return "L"
    if ref_high is not None and value > ref_high:
        return "H"
    return ""


@router.post("/bloodwork/result")
async def save_result(
    request: Request,
    marker_id: int = Form(...),
    date: str = Form(...),
    value: float = Form(...),
    value_text: str = Form(""),
    flag: str = Form(""),
):
    date = valid_date(date)
    if not date:
        return error_redirect(request, "/bloodwork", _("Choose a valid bloodwork date."))
    if flag and flag not in ("", "H", "L"):
        return error_redirect(request, "/bloodwork", _("Choose a valid bloodwork result flag."))
    if not isfinite(value):
        return error_redirect(request, "/bloodwork", _("Enter the result as a number."))
    value_text = truncate(value_text, 200)
    db = get_user_db_from_request(request)

    # Validate the marker exists — a stale/forged id otherwise surfaces as a
    # foreign-key 500 instead of a controlled redirect.
    marker_rows = await db.execute_fetchall("SELECT ref_low, ref_high FROM blood_markers WHERE id = ?", (marker_id,))
    if not marker_rows:
        return error_redirect(request, "/bloodwork", _("Blood marker was not found."))

    # No explicit flag → derive it from the stored reference range, so the
    # lab-reported value can still override (labs use their own ranges). Here an
    # empty flag means "derive it"; the edit form says "auto" for the same thing,
    # because there an empty flag is a lab saying "normal". A text result
    # ("negative") is not flagged from the number stored beside it.
    if not flag and not value_text:
        marker = dict(marker_rows[0])
        flag = compute_flag(value, marker["ref_low"], marker["ref_high"])

    await db.execute(
        """
        INSERT INTO blood_results (marker_id, date, value, value_text, flag)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(marker_id, date) DO UPDATE SET
            value=excluded.value, value_text=excluded.value_text, flag=excluded.flag
    """,
        (marker_id, date, value, value_text, flag),
    )
    await db.commit()
    return success_redirect(request, "/bloodwork", _("Blood result saved."))


@router.post("/bloodwork/marker")
async def save_marker(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    unit: str = Form(...),
    ref_low: OptionalFormFloat = None,
    ref_high: OptionalFormFloat = None,
    display_order: int = Form(0),
):
    name = name.strip()
    category = category.strip()
    unit = unit.strip()
    if not name or not category or not unit:
        return error_redirect(request, "/bloodwork", _("Name, category, and unit are required."))
    if ref_low is not None and ref_high is not None and ref_low > ref_high:
        return error_redirect(request, "/bloodwork", _("Reference minimum cannot exceed the maximum."))
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO blood_markers (name, category, unit, ref_low, ref_high, display_order)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            category=excluded.category, unit=excluded.unit,
            ref_low=excluded.ref_low, ref_high=excluded.ref_high,
            display_order=excluded.display_order
    """,
        (name, category, unit, ref_low, ref_high, display_order),
    )
    await db.commit()
    return success_redirect(request, "/bloodwork", _("Blood marker saved."))


def _marker_page(category: str, marker_id: int) -> str:
    """Back to the marker's results on the tab the form came from."""
    return f"/bloodwork/{quote(category or 'all', safe='')}?marker={marker_id}#marker-results"


@router.post("/bloodwork/result/{result_id}/edit")
async def edit_result(
    request: Request,
    result_id: int,
    date: str = Form(...),
    value: float = Form(...),
    value_text: str = Form(""),
    flag: str = Form("auto"),
    category: str = Form("all"),
):
    """Correct one result: its date, value, text or flag.

    `flag` is "auto" (derive it from the marker's reference range, as a new
    result does), "", "H" or "L".
    """
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall(
        "SELECT r.marker_id, m.ref_low, m.ref_high FROM blood_results r JOIN blood_markers m ON m.id = r.marker_id "
        "WHERE r.id = ?",
        (result_id,),
    )
    if not rows:
        return error_redirect(request, "/bloodwork", _("That blood result no longer exists."))
    marker = dict(rows[0])
    destination = _marker_page(category, marker["marker_id"])
    date = valid_date(date)
    if not date:
        return error_redirect(request, destination, _("Choose a valid bloodwork date."))
    if not isfinite(value):
        return error_redirect(request, destination, _("Enter the result as a number."))
    if flag == "auto":
        flag = "" if value_text.strip() else compute_flag(value, marker["ref_low"], marker["ref_high"])
    elif flag not in ("", "H", "L"):
        return error_redirect(request, destination, _("Choose a valid bloodwork result flag."))
    try:
        await db.execute(
            "UPDATE blood_results SET date = ?, value = ?, value_text = ?, flag = ? WHERE id = ?",
            (date, value, truncate(value_text, 200), flag, result_id),
        )
    except sqlite3.IntegrityError:
        await db.rollback()
        return error_redirect(
            request,
            destination,
            _("This marker already has a result on {date}. Edit or delete that one instead.").format(date=date),
        )
    await db.commit()
    return success_redirect(request, destination, _("Blood result corrected."))


@router.post("/bloodwork/result/{result_id}/delete")
async def delete_result(request: Request, result_id: int, category: str = Form("all")):
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT marker_id FROM blood_results WHERE id = ?", (result_id,))
    if not rows:
        return error_redirect(request, "/bloodwork", _("That blood result no longer exists."))
    await db.execute("DELETE FROM blood_results WHERE id = ?", (result_id,))
    await db.commit()
    return success_redirect(request, _marker_page(category, rows[0]["marker_id"]), _("Blood result deleted."))
