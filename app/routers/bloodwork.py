from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormFloat, truncate, valid_date

router = APIRouter()


def _as_display(row) -> str:
    """A result as the page prints it: the text form wins over the number."""
    if row.get("value_text"):
        return str(row["value_text"])
    value = row.get("value")
    if value is None:
        return ""
    # Trim a trailing .0 so 5.0 reads as 5, the way the matrix already does.
    return f"{value:g}"


def marker_summary(marker_results: dict) -> dict:
    """Latest value, its flag, and the change from the result before it.

    The mobile list answers one question - "where is this marker now, and which
    way did it move" - which the full date matrix can only answer by scrolling
    sideways past three columns of metadata.

    `change` stays empty unless both results are numeric. blood_results.value is
    REAL NOT NULL, so a text result ("negative", "traces") still stores a number;
    value_text is what says the number is not the result, and subtracting those
    numbers would report a direction the user never recorded.
    """
    empty = {"date": "", "value": "", "flag": "", "previous": "", "change": ""}
    if not marker_results:
        return empty

    dates = sorted(marker_results)
    latest = marker_results[dates[-1]]
    previous = marker_results[dates[-2]] if len(dates) > 1 else None

    summary = {
        "date": dates[-1],
        "value": _as_display(latest),
        "flag": latest.get("flag") or "",
        "previous": _as_display(previous) if previous else "",
        "change": "",
    }
    numeric = (
        previous is not None
        and not latest.get("value_text")
        and not previous.get("value_text")
        and latest.get("value") is not None
        and previous.get("value") is not None
    )
    if numeric:
        summary["change"] = f"{float(latest['value']) - float(previous['value']):+.1f}"
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
        m["summary"] = marker_summary(results.get(m["id"], {}))

    # Selected marker for chart (first one or from query param)
    chart_marker_id = request.query_params.get("marker")
    chart_marker = None
    chart_labels = []
    chart_values = []
    chart_ref_low = None
    chart_ref_high = None
    if chart_marker_id and chart_marker_id.isdigit():
        mid = int(chart_marker_id)
        m_row = await db.execute_fetchall("SELECT * FROM blood_markers WHERE id = ?", (mid,))
        if m_row:
            chart_marker = dict(m_row[0])
            chart_ref_low = chart_marker.get("ref_low")
            chart_ref_high = chart_marker.get("ref_high")
            if mid in results:
                for d in sorted(results[mid].keys()):
                    chart_labels.append(d)
                    chart_values.append(results[mid][d]["value"])

    return templates.TemplateResponse(
        "bloodwork.html",
        {
            "request": request,
            "categories": categories,
            "current_category": category,
            "markers": markers,
            "test_dates": test_dates,
            "results": results,
            "chart_marker": chart_marker,
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
    if not valid_date(date):
        return RedirectResponse("/bloodwork", status_code=303)
    if flag and flag not in ("", "H", "L"):
        flag = ""
    value_text = truncate(value_text, 200)
    db = get_user_db_from_request(request)

    # Validate the marker exists — a stale/forged id otherwise surfaces as a
    # foreign-key 500 instead of a controlled redirect.
    marker_rows = await db.execute_fetchall("SELECT ref_low, ref_high FROM blood_markers WHERE id = ?", (marker_id,))
    if not marker_rows:
        return RedirectResponse("/bloodwork", status_code=303)

    # No explicit flag → derive it from the stored reference range, so the
    # lab-reported value can still override (labs use their own ranges).
    if not flag:
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
    return RedirectResponse("/bloodwork", status_code=303)


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
    return RedirectResponse("/bloodwork", status_code=303)
