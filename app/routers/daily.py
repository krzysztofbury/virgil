import logging
import secrets
from datetime import date as date_module
from datetime import timedelta

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.daily_log import DailyLogError, cycle_status, day_progress, upsert_daily_log
from app.services.llm import llm_available
from app.services.measurements import MeasurementError, upsert_measurements
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormInt, truncate, valid_date

router = APIRouter()
logger = logging.getLogger(__name__)

ANDY_JOB_KIND = "andy_generation"

DAYS_PL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# Three routines and four A.N.D.Y. tasks. The page stated neither a count nor a
# total, so "how much of today is left" had to be read off the icons.
DONE_FIELDS = (
    "morning_routine",
    "evening_routine",
    "water",
    "andy_body_status",
    "andy_spirit_status",
    "andy_account_status",
    "andy_relations_status",
)


@router.get("/daily", response_class=HTMLResponse)
@router.get("/daily/{day}", response_class=HTMLResponse)
async def daily_page(request: Request, day: str | None = None, job_id: int | None = Query(None, ge=1)):
    try:
        target = date_module.fromisoformat(day) if day else date_module.today()
    except (ValueError, TypeError):
        return RedirectResponse("/daily", status_code=303)
    db = get_user_db_from_request(request)

    row = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (target.isoformat(),))
    log = dict(row[0]) if row else None

    meas_row = await db.execute_fetchall("SELECT * FROM body_measurements WHERE date = ?", (target.isoformat(),))
    measurements = dict(meas_row[0]) if meas_row else None

    is_saturday = target.weekday() == 5

    prev_day = (target - timedelta(days=1)).isoformat()
    next_day = (target + timedelta(days=1)).isoformat()
    day_name = DAYS_PL[target.weekday()]

    llm_configured = await llm_available(db)

    from app.routers.jobs import current_job_view

    current_job = await current_job_view(db, job_id)

    # Per-habit current streaks
    habit_fields = [
        ("morning_routine", "Morning Routine"),
        ("evening_routine", "Evening Routine"),
        ("water", "Water"),
        ("andy_body_status", "Body"),
        ("andy_spirit_status", "Spirit"),
        ("andy_account_status", "Self"),
        ("andy_relations_status", "Relations"),
    ]
    all_logs = await db.execute_fetchall(
        "SELECT date, morning_routine, evening_routine, water, "
        "andy_body_status, andy_spirit_status, andy_account_status, andy_relations_status "
        "FROM daily_logs ORDER BY date DESC LIMIT 90"
    )
    habit_streaks = {}
    today_str = date_module.today().isoformat()
    for field, label in habit_fields:
        streak = 0
        expected = None
        for row in all_logs:
            row_date = row["date"]
            # Skip today if this field isn't done yet (day not over)
            if row_date == today_str and row[field] != "done":
                continue
            d = date_module.fromisoformat(row_date)
            if expected is None:
                expected = d
            if d != expected:
                break  # Gap in dates breaks the streak
            if row[field] == "done":
                streak += 1
                expected = d - timedelta(days=1)
            else:
                break
        habit_streaks[field] = {"label": label, "streak": streak}

    # Heatmap data: last 7 days of daily completion
    day_short_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    seven_days_ago = (target - timedelta(days=6)).isoformat()
    heatmap_rows = await db.execute_fetchall(
        "SELECT date, morning_routine, evening_routine, water, "
        "andy_body_status, andy_spirit_status, andy_account_status, andy_relations_status "
        "FROM daily_logs WHERE date >= ? AND date <= ? ORDER BY date",
        (seven_days_ago, target.isoformat()),
    )
    heatmap_by_date = {}
    for row in heatmap_rows:
        statuses = [
            row["morning_routine"],
            row["evening_routine"],
            row["water"],
            row["andy_body_status"],
            row["andy_spirit_status"],
            row["andy_account_status"],
            row["andy_relations_status"],
        ]
        done = sum(1 for s in statuses if s == "done")
        pct = round(done / 7 * 100)
        heatmap_by_date[row["date"]] = pct
    heatmap_data = []
    for i in range(7):
        d = target - timedelta(days=6 - i)
        d_iso = d.isoformat()
        heatmap_data.append(
            {
                "date": d_iso,
                "pct": heatmap_by_date.get(d_iso, 0),
                "day_short": day_short_names[d.weekday()],
            }
        )

    return templates.TemplateResponse(
        request,
        "daily.html",
        {
            "request": request,
            "date": target.isoformat(),
            "day_name": day_name,
            "prev_day": prev_day,
            "next_day": next_day,
            "log": log,
            "measurements": measurements,
            "is_saturday": is_saturday,
            "llm_configured": llm_configured,
            "current_job": current_job,
            "job_nonce": secrets.token_hex(16),
            "habit_streaks": habit_streaks,
            "heatmap_data": heatmap_data,
            "done_count": sum(1 for field in DONE_FIELDS if log and log[field] == "done"),
            "done_total": len(DONE_FIELDS),
        },
    )


@router.post("/daily/{day}/cycle", response_class=HTMLResponse)
async def cycle_daily_status(request: Request, day: str, field: str = Form(...)):
    """Advance one status of a day from wherever it is shown (the dashboard's
    Today block) and return that block redrawn.

    A partial write on purpose: /daily/save replaces the whole day, so a tap
    there would reset every field the tapping page did not post.
    """
    if not valid_date(day):
        return Response(
            status_code=422, headers={"X-Feedback-Message": "Invalid daily-log date.", "X-Feedback-Kind": "error"}
        )
    try:
        log = await cycle_status(get_user_db_from_request(request), day, field)
    except DailyLogError as exc:
        return Response(status_code=422, headers={"X-Feedback-Message": str(exc), "X-Feedback-Kind": "error"})
    return templates.TemplateResponse(
        request,
        "partials/today_block.html",
        {"request": request, "today": day, "today_log": log, **day_progress(log)},
    )


@router.post("/daily/save")
async def save_daily(
    request: Request,
    date: str = Form(...),
    energy: OptionalFormInt = None,
    morning_routine: str = Form("pending"),
    evening_routine: str = Form("pending"),
    water: str = Form("pending"),
    andy_body_status: str = Form("pending"),
    andy_body_desc: str = Form(""),
    andy_spirit_status: str = Form("pending"),
    andy_spirit_desc: str = Form(""),
    andy_account_status: str = Form("pending"),
    andy_account_desc: str = Form(""),
    andy_relations_status: str = Form("pending"),
    andy_relations_desc: str = Form(""),
    notes: str = Form(""),
):
    if not valid_date(date):
        return error_redirect(request, "/daily", "Invalid daily-log date.")
    # Blank means the day was not rated. A number is still clamped into 1-10.
    if energy is not None:
        energy = max(1, min(10, energy))
    andy_body_desc = truncate(andy_body_desc, 500)
    andy_spirit_desc = truncate(andy_spirit_desc, 500)
    andy_account_desc = truncate(andy_account_desc, 500)
    andy_relations_desc = truncate(andy_relations_desc, 500)
    notes = truncate(notes)
    valid_statuses = ("done", "skipped", "pending")
    morning_routine = morning_routine if morning_routine in valid_statuses else "pending"
    evening_routine = evening_routine if evening_routine in valid_statuses else "pending"
    water = water if water in valid_statuses else "pending"
    andy_body_status = andy_body_status if andy_body_status in valid_statuses else "pending"
    andy_spirit_status = andy_spirit_status if andy_spirit_status in valid_statuses else "pending"
    andy_account_status = andy_account_status if andy_account_status in valid_statuses else "pending"
    andy_relations_status = andy_relations_status if andy_relations_status in valid_statuses else "pending"
    db = get_user_db_from_request(request)
    await upsert_daily_log(
        db,
        date,
        {
            "energy": energy,
            "morning_routine": morning_routine,
            "evening_routine": evening_routine,
            "water": water,
            "andy_body_status": andy_body_status,
            "andy_body_desc": andy_body_desc,
            "andy_spirit_status": andy_spirit_status,
            "andy_spirit_desc": andy_spirit_desc,
            "andy_account_status": andy_account_status,
            "andy_account_desc": andy_account_desc,
            "andy_relations_status": andy_relations_status,
            "andy_relations_desc": andy_relations_desc,
            "notes": notes,
        },
    )

    if request.headers.get("HX-Request"):
        return Response(
            status_code=200,
            headers={
                "X-Feedback-Message": "Saved",
                "X-Feedback-Kind": "success",
                "X-Draft-Clear": f"daily:{date}",
            },
        )
    return success_redirect(request, f"/daily/{date}", "Daily log saved.", clear_draft=f"daily:{date}")


@router.post("/daily/generate-andy")
async def generate_andy(request: Request, date: str = Form(...), job_nonce: str = Form(...)):
    """Queue the suggestions. The provider call belongs to the worker."""
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key

    if not valid_date(date):
        return error_redirect(request, "/daily", "Invalid planning date.")
    day = date_module.fromisoformat(date).isoformat()

    db = get_user_db_from_request(request)
    if not await llm_available(db):
        return error_redirect(request, f"/daily/{day}", "No AI provider is configured. Add one in Settings.")
    try:
        key = paid_llm_job_key(ANDY_JOB_KIND, day, job_nonce)
    except ValueError:
        return error_redirect(request, f"/daily/{day}", "Reload the page and try again.")
    try:
        result = await enqueue_paid_llm_job(
            db,
            ANDY_JOB_KIND,
            {"day": day, "key_part": job_nonce},
            idempotency_key=key,
        )
    except ActiveWorkloadConflictError:
        return error_redirect(request, f"/daily/{day}", "A.N.D.Y. suggestions are already queued.")
    except Exception:
        logger.exception("A.N.D.Y. enqueue failed")
        return error_redirect(request, f"/daily/{day}", "The suggestions could not be queued. Try again.")
    from app.routers.jobs import wake_worker_for

    wake_worker_for(request)

    return success_redirect(request, f"/daily/{day}?job_id={result.job_id}", "A.N.D.Y. suggestions queued.")


@router.post("/daily/measurements")
async def save_measurements(
    request: Request,
    date: str = Form(...),
    weight: str = Form(""),
    arm: str = Form(""),
    waist: str = Form(""),
    hips: str = Form(""),
    thighs: str = Form(""),
):
    if not valid_date(date):
        return error_redirect(request, "/daily", "Invalid measurement date.")

    def to_float(v: str) -> float | None:
        try:
            return float(v) if v else None
        except ValueError:
            return None

    db = get_user_db_from_request(request)
    values = {"weight": weight, "arm": arm, "waist": waist, "hips": hips, "thighs": thighs}
    try:
        await upsert_measurements(db, date, {key: to_float(value) for key, value in values.items()})
    except MeasurementError as exc:
        return error_redirect(request, f"/daily/{date}", str(exc))
    if request.headers.get("HX-Request"):
        return Response(
            status_code=200,
            headers={"X-Feedback-Message": "Measurements saved", "X-Feedback-Kind": "success"},
        )
    return success_redirect(request, f"/daily/{date}", "Measurements saved.")
