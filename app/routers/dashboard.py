import calendar
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db import LIFE_AREA_LABELS, LIFE_AREAS, get_setting
from app.main import templates
from app.services.streak import get_streak
from app.user_db import get_user_db_from_request

logger = logging.getLogger(__name__)

router = APIRouter()

AREAS = LIFE_AREAS
AREA_LABELS = LIFE_AREA_LABELS


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    db = get_user_db_from_request(request)
    today = date.today()

    # Today's log
    row = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (today.isoformat(),))
    today_log = dict(row[0]) if row else None

    # Week overview (Mon-Sun of current week)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    week_dates = [(monday + timedelta(days=i)) for i in range(7)]
    week_rows = await db.execute_fetchall(
        "SELECT date, energy, andy_body_status, andy_spirit_status, "
        "andy_account_status, andy_relations_status, morning_routine, "
        "evening_routine, water FROM daily_logs WHERE date BETWEEN ? AND ?",
        (monday.isoformat(), sunday.isoformat()),
    )
    logs_by_date = {row["date"]: dict(row) for row in week_rows}
    week_logs = []
    for d in week_dates:
        log = logs_by_date.get(d.isoformat())
        if log:
            statuses = [
                log.get("andy_body_status"),
                log.get("andy_spirit_status"),
                log.get("andy_account_status"),
                log.get("andy_relations_status"),
                log.get("morning_routine"),
                log.get("evening_routine"),
                log.get("water"),
            ]
            done_count = sum(1 for s in statuses if s == "done")
            log["completion"] = round(done_count / len(statuses) * 100)
            week_logs.append(log)
        else:
            week_logs.append({"date": d.isoformat(), "energy": None, "completion": 0})

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Latest body measurements (prefer body_measurements, fallback to life_scores)
    meas = await db.execute_fetchall("SELECT * FROM body_measurements ORDER BY date DESC LIMIT 2")
    latest_meas = dict(meas[0]) if meas else None
    prev_meas = dict(meas[1]) if len(meas) > 1 else None

    if not latest_meas:
        ls_meas = await db.execute_fetchall(
            "SELECT date, weight, waist FROM life_scores WHERE weight IS NOT NULL ORDER BY date DESC LIMIT 2"
        )
        if ls_meas:
            latest_meas = dict(ls_meas[0])
            prev_meas = dict(ls_meas[1]) if len(ls_meas) > 1 else None

    # Feniks streak (skip if feature disabled)
    features = getattr(request.state, "features", {})
    if features.get("no_porn"):
        feniks_conf = await db.execute_fetchall("SELECT * FROM feniks_config WHERE id = 1")
        feniks_config = dict(feniks_conf[0]) if feniks_conf else None
        streak_days, _ = await get_streak(db)
        feniks_progress = min(100, round(streak_days / feniks_config["target_days"] * 100)) if feniks_config else 0
    else:
        feniks_config = None
        streak_days = 0
        feniks_progress = 0

    # Oura latest (prefer today's daily data, fallback to monthly)
    oura_daily_row = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date = ?", (today.isoformat(),))
    oura_today = dict(oura_daily_row[0]) if oura_daily_row else None

    # Yesterday fallback for Activity/Steps (often missing early in the day)
    yesterday_fallback = {}
    if oura_today and (oura_today.get("activity_score") is None or oura_today.get("steps") is None):
        yesterday_str = (today - timedelta(days=1)).isoformat()
        yd_row = await db.execute_fetchall(
            "SELECT activity_score, steps FROM oura_daily WHERE date = ?", (yesterday_str,)
        )
        if yd_row:
            yd = dict(yd_row[0])
            if oura_today.get("activity_score") is None and yd.get("activity_score") is not None:
                yesterday_fallback["activity_score"] = yd["activity_score"]
            if oura_today.get("steps") is None and yd.get("steps") is not None:
                yesterday_fallback["steps"] = yd["steps"]

    oura = await db.execute_fetchall("SELECT * FROM oura_monthly ORDER BY month DESC LIMIT 1")
    latest_oura = dict(oura[0]) if oura else None

    # Life scores (latest 2 for radar chart comparison)
    score_rows = await db.execute_fetchall("SELECT * FROM life_scores ORDER BY date DESC LIMIT 2")
    scores = [dict(r) for r in score_rows]

    chart_datasets = []
    colors = ["rgba(168, 85, 247, 0.5)", "rgba(59, 130, 246, 0.3)"]
    border_colors = ["rgb(168, 85, 247)", "rgb(59, 130, 246)"]
    for i, s in enumerate(scores[:2]):
        chart_datasets.append(
            {
                "label": s["date"],
                "data": [s.get(a) or 0 for a in AREAS],
                "backgroundColor": colors[i] if i < len(colors) else "rgba(100,100,100,0.3)",
                "borderColor": border_colors[i] if i < len(border_colors) else "rgb(100,100,100)",
            }
        )

    # Year calendar (dot-matrix by month)
    current_year = today.year
    year_months = []
    days_passed = (today - date(current_year, 1, 1)).days
    days_total = (date(current_year, 12, 31) - date(current_year, 1, 1)).days + 1
    days_left = days_total - days_passed - 1
    year_pct = round(days_passed / days_total * 100)

    # Build per-day completion data for the year
    year_logs = await db.execute_fetchall(
        "SELECT date, morning_routine, evening_routine, water, "
        "andy_body_status, andy_spirit_status, andy_account_status, andy_relations_status "
        "FROM daily_logs WHERE date >= ? AND date <= ?",
        (date(current_year, 1, 1).isoformat(), date(current_year, 12, 31).isoformat()),
    )
    completion_map: dict[str, dict] = {}
    for row in year_logs:
        andy = [
            row["andy_body_status"],
            row["andy_spirit_status"],
            row["andy_account_status"],
            row["andy_relations_status"],
        ]
        routines = [row["morning_routine"], row["evening_routine"], row["water"]]
        andy_done = sum(1 for s in andy if s == "done")
        routines_done = sum(1 for s in routines if s == "done")
        pct = round((andy_done + routines_done) / 7 * 100)
        completion_map[row["date"]] = {"pct": pct, "andy": andy_done, "routines": routines_done}

    for m in range(1, 13):
        num_days = calendar.monthrange(current_year, m)[1]
        # weekday of 1st: 0=Mon..6=Sun
        first_weekday = date(current_year, m, 1).weekday()
        days = []
        for d in range(1, num_days + 1):
            day_date = date(current_year, m, d)
            day_iso = day_date.isoformat()
            detail = completion_map.get(day_iso, {})
            pct = detail.get("pct", 0)
            if day_date < today:
                if pct >= 75:
                    status = "good"
                elif pct > 0:
                    status = "low"
                else:
                    status = "past"
            elif day_date == today:
                status = "today"
            else:
                status = "future"
            days.append(
                {
                    "day": d,
                    "date": day_iso,
                    "status": status,
                    "pct": pct,
                    "andy": detail.get("andy", 0),
                    "routines": detail.get("routines", 0),
                }
            )
        year_months.append(
            {
                "name": calendar.month_abbr[m],
                "first_weekday": first_weekday,
                "days": days,
            }
        )

    # Active experiments
    active_experiments = []
    exp_rows = await db.execute_fetchall(
        "SELECT * FROM experiments WHERE status = 'active' ORDER BY created_at DESC LIMIT 3"
    )
    for er in exp_rows:
        exp = dict(er)
        start = date.fromisoformat(exp["start_date"])
        end = start + timedelta(weeks=exp["num_weeks"])
        if today < start:
            current_week = 0
            exp["not_started"] = True
        else:
            current_week = min(exp["num_weeks"], (today - start).days // 7 + 1)
            exp["not_started"] = False
        total_row = await db.execute_fetchall(
            "SELECT COALESCE(SUM(duration_minutes), 0) as total FROM experiment_entries WHERE experiment_id = ?",
            (exp["id"],),
        )
        exp["total_minutes"] = total_row[0]["total"] if total_row else 0
        exp["current_week"] = current_week
        exp["end_date"] = end.isoformat()
        active_experiments.append(exp)

    # Morning briefing
    briefing_enabled = await get_setting(db, "briefing_enabled", "0") == "1"
    briefing_text = None
    if briefing_enabled:
        from app.services.briefing import get_cached_briefing

        briefing_text = await get_cached_briefing(db)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "today": today.isoformat(),
            "briefing_enabled": briefing_enabled,
            "briefing_text": briefing_text,
            "today_log": today_log,
            "week_logs": week_logs,
            "day_names": day_names,
            "week_dates": [d.isoformat() for d in week_dates],
            "latest_meas": latest_meas,
            "prev_meas": prev_meas,
            "streak_days": streak_days,
            "feniks_config": feniks_config,
            "feniks_progress": feniks_progress,
            "oura_today": oura_today,
            "yesterday_fallback": yesterday_fallback,
            "latest_oura": latest_oura,
            "scores": scores,
            "areas": AREAS,
            "area_labels": AREA_LABELS,
            "chart_datasets": chart_datasets,
            "year_months": year_months,
            "current_year": current_year,
            "days_left": days_left,
            "year_pct": year_pct,
            "active_experiments": active_experiments,
        },
    )


@router.get("/offline", response_class=HTMLResponse)
async def offline_page(request: Request):
    return templates.TemplateResponse("offline.html", {"request": request})


@router.post("/api/briefing/generate", response_class=HTMLResponse)
async def generate_briefing_endpoint(request: Request):
    from app.services.briefing import generate_briefing

    db = get_user_db_from_request(request)
    try:
        content = await generate_briefing(db)
        return templates.TemplateResponse(
            "partials/briefing_card.html",
            {"request": request, "briefing_text": content},
        )
    except Exception:
        logger.exception("Briefing generation failed")
        return HTMLResponse(
            '<div class="text-muted" style="padding:0.5rem;">'
            "Failed to generate briefing. Check LLM provider settings.</div>",
            status_code=200,
        )
