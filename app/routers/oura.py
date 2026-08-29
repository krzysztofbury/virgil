import logging
from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormFloat, OptionalFormInt, valid_month

logger = logging.getLogger(__name__)


router = APIRouter()

METRICS = [
    ("sleep_score", "Sleep Score", ""),
    ("readiness", "Readiness", ""),
    ("activity", "Activity", ""),
    ("steps", "Steps", ""),
    ("sleep_duration", "Sleep Duration", "h"),
    ("deep_sleep", "Deep Sleep", "h"),
    ("rem_sleep", "REM Sleep", "h"),
    ("rhr", "Resting HR", "bpm"),
    ("lowest_hr", "Lowest HR", "bpm"),
    ("hrv", "HRV", "ms"),
    ("cardiovascular_age", "Cardio Age", ""),
]


BASELINE_DAYS = 7
# Oura's own scores move a few points day to day without meaning anything. Below
# this the honest word is "steady", which is why a threshold exists rather than a
# strict comparison.
BASELINE_TOLERANCE = 3


def readiness_baseline(rows: list[dict], today_score: int | None) -> dict:
    """Today's readiness against the mean of the previous BASELINE_DAYS days.

    A rule, not an interpretation: the page states the two numbers, their
    difference and one word. Nothing here generates advice and nothing calls an
    LLM - a page that states a verdict was explicitly not wanted.

    `rows` must exclude today and be newest first. A day with no readiness score
    is skipped, so a gap shortens the window instead of padding it with zero.
    """
    scores = [r["readiness_score"] for r in rows if r.get("readiness_score") is not None][:BASELINE_DAYS]
    if not scores or today_score is None:
        return {"today": today_score, "baseline": None, "delta": None, "status": "", "days": len(scores)}

    baseline = round(sum(scores) / len(scores))
    delta = today_score - baseline
    if delta >= BASELINE_TOLERANCE:
        status = "above"
    elif delta <= -BASELINE_TOLERANCE:
        status = "below"
    else:
        status = "steady"
    return {"today": today_score, "baseline": baseline, "delta": delta, "status": status, "days": len(scores)}


@router.get("/oura", response_class=HTMLResponse)
@router.get("/oura/{metric}", response_class=HTMLResponse)
async def oura_page(request: Request, metric: str = "sleep_score"):
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT * FROM oura_monthly ORDER BY month")
    data = [dict(r) for r in rows]

    labels = [d["month"] for d in data]
    values = [d.get(metric) for d in data]

    metric_info = next((m for m in METRICS if m[0] == metric), METRICS[0])

    # Check Oura connection status
    oura_row = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
    oura_connected = oura_row[0]["status"] == "connected" if oura_row else False

    # Today's daily data (fall back to yesterday for activity/steps)
    today_str = date.today().isoformat()
    today_row = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date = ?", (today_str,))
    oura_today = dict(today_row[0]) if today_row else None
    yesterday_fallback = {}
    if oura_today and (oura_today.get("activity_score") is None or oura_today.get("steps") is None):
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()
        yd_row = await db.execute_fetchall(
            "SELECT activity_score, steps FROM oura_daily WHERE date = ?", (yesterday_str,)
        )
        if yd_row:
            yd = dict(yd_row[0])
            if oura_today.get("activity_score") is None and yd.get("activity_score") is not None:
                yesterday_fallback["activity_score"] = yd["activity_score"]
            if oura_today.get("steps") is None and yd.get("steps") is not None:
                yesterday_fallback["steps"] = yd["steps"]

    # Daily data for browsable table (last 30 days)
    daily_rows = await db.execute_fetchall("SELECT * FROM oura_daily ORDER BY date DESC LIMIT 30")
    daily_data = [dict(r) for r in daily_rows]

    # daily_data is newest first and includes today; the baseline compares against
    # the days BEFORE today, so today's row is dropped.
    baseline = readiness_baseline(
        [r for r in daily_data if r["date"] != today_str],
        oura_today.get("readiness_score") if oura_today else None,
    )

    # Daily trends (last 10 days)
    ten_days_ago = (date.today() - timedelta(days=10)).isoformat()
    trend_rows = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date >= ? ORDER BY date", (ten_days_ago,))
    trend_data = [dict(r) for r in trend_rows]
    trend_labels = [r["date"][5:] for r in trend_data]  # MM-DD format
    trend_hrv = [r.get("avg_hrv") for r in trend_data]
    trend_sleep = [r.get("sleep_score") for r in trend_data]
    trend_readiness = [r.get("readiness_score") for r in trend_data]
    trend_rhr = [r.get("resting_hr") for r in trend_data]

    return templates.TemplateResponse(
        "oura.html",
        {
            "request": request,
            "metrics": METRICS,
            "current_metric": metric,
            "metric_name": metric_info[1],
            "metric_unit": metric_info[2],
            "labels": labels,
            "values": values,
            "data": data,
            "oura_connected": oura_connected,
            "oura_today": oura_today,
            "baseline": baseline,
            "yesterday_fallback": yesterday_fallback,
            "daily_data": daily_data,
            "trend_labels": trend_labels,
            "trend_hrv": trend_hrv,
            "trend_sleep": trend_sleep,
            "trend_readiness": trend_readiness,
            "trend_rhr": trend_rhr,
        },
    )


@router.post("/oura/save")
async def save_oura(
    request: Request,
    month: str = Form(...),
    sleep_score: OptionalFormFloat = None,
    readiness: OptionalFormFloat = None,
    activity: OptionalFormFloat = None,
    steps: OptionalFormInt = None,
    sleep_duration: OptionalFormFloat = None,
    deep_sleep: OptionalFormFloat = None,
    rem_sleep: OptionalFormFloat = None,
    rhr: OptionalFormFloat = None,
    lowest_hr: OptionalFormFloat = None,
    hrv: OptionalFormFloat = None,
    cardiovascular_age: OptionalFormInt = None,
    stress_normal: OptionalFormInt = None,
    stress_stressful: OptionalFormInt = None,
    stress_restored: OptionalFormInt = None,
    notes: str = Form(""),
):
    if not valid_month(month):
        return error_redirect(request, "/oura", "Invalid Oura month.")
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO oura_monthly (month, sleep_score, readiness, activity, steps,
            sleep_duration, deep_sleep, rem_sleep, rhr, lowest_hr, hrv,
            cardiovascular_age, stress_normal, stress_stressful, stress_restored, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(month) DO UPDATE SET
            sleep_score=excluded.sleep_score, readiness=excluded.readiness,
            activity=excluded.activity, steps=excluded.steps,
            sleep_duration=excluded.sleep_duration, deep_sleep=excluded.deep_sleep,
            rem_sleep=excluded.rem_sleep, rhr=excluded.rhr, lowest_hr=excluded.lowest_hr,
            hrv=excluded.hrv, cardiovascular_age=excluded.cardiovascular_age,
            stress_normal=excluded.stress_normal, stress_stressful=excluded.stress_stressful,
            stress_restored=excluded.stress_restored, notes=excluded.notes
    """,
        (
            month,
            sleep_score,
            readiness,
            activity,
            steps,
            sleep_duration,
            deep_sleep,
            rem_sleep,
            rhr,
            lowest_hr,
            hrv,
            cardiovascular_age,
            stress_normal,
            stress_stressful,
            stress_restored,
            notes,
        ),
    )
    await db.commit()
    return success_redirect(request, "/oura", "Oura month saved.")


@router.post("/oura/delete")
async def delete_oura(request: Request, month: str = Form(...)):
    if not valid_month(month):
        return error_redirect(request, "/oura", "Invalid Oura month.")
    db = get_user_db_from_request(request)
    cursor = await db.execute("DELETE FROM oura_monthly WHERE month = ?", (month,))
    if cursor.rowcount != 1:
        await db.rollback()
        return error_redirect(request, "/oura", "Oura month not found.")
    await db.commit()
    return success_redirect(request, "/oura", "Oura month deleted.")


@router.post("/oura/api-sync")
async def oura_api_sync(request: Request):
    from app.services.oura_api import sync_oura_from_api

    db = get_user_db_from_request(request)
    try:
        result = await sync_oura_from_api(db)
        logger.info("Oura API sync from oura page: %d days", result.days)
    except Exception:
        logger.exception("Oura API sync failed")
        return error_redirect(request, "/oura", "Oura sync failed. Try again.")
    if not result.complete:
        return error_redirect(
            request,
            "/oura",
            "Oura sync was partial. Existing data for failed sources was preserved.",
        )
    day_word = "day" if result.days == 1 else "days"
    return success_redirect(request, "/oura", f"Oura sync completed: {result.days} {day_word}")
