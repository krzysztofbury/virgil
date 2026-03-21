from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.main import templates
from app.validation import truncate, valid_date

router = APIRouter()


@router.get("/bloodwork", response_class=HTMLResponse)
@router.get("/bloodwork/{category}", response_class=HTMLResponse)
async def bloodwork_page(request: Request, category: str = "all"):
    db = await get_db()

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
    db = await get_db()
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
    ref_low: float | None = Form(None),
    ref_high: float | None = Form(None),
    display_order: int = Form(0),
):
    db = await get_db()
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
