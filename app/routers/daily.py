import logging
import os
from datetime import date as date_module
from datetime import timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from app.config import SECOND_BRAIN_PATH
from app.db import get_db
from app.main import templates
from app.services.llm import get_active_provider
from app.validation import truncate, valid_date

router = APIRouter()
logger = logging.getLogger(__name__)

DAYS_PL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@router.get("/daily", response_class=HTMLResponse)
@router.get("/daily/{day}", response_class=HTMLResponse)
async def daily_page(request: Request, day: str | None = None):
    try:
        target = date_module.fromisoformat(day) if day else date_module.today()
    except (ValueError, TypeError):
        return RedirectResponse("/daily", status_code=303)
    db = await get_db()

    row = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (target.isoformat(),))
    log = dict(row[0]) if row else None

    meas_row = await db.execute_fetchall("SELECT * FROM body_measurements WHERE date = ?", (target.isoformat(),))
    measurements = dict(meas_row[0]) if meas_row else None

    is_saturday = target.weekday() == 5

    prev_day = (target - timedelta(days=1)).isoformat()
    next_day = (target + timedelta(days=1)).isoformat()
    day_name = DAYS_PL[target.weekday()]

    llm_configured = await get_active_provider(db) is not None

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
            "habit_streaks": habit_streaks,
            "heatmap_data": heatmap_data,
        },
    )


@router.post("/daily/save")
async def save_daily(
    request: Request,
    date: str = Form(...),
    energy: int = Form(0),
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
        return RedirectResponse("/daily", status_code=303)
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
    db = await get_db()
    await db.execute(
        """
        INSERT INTO daily_logs (date, energy, morning_routine, evening_routine, water,
            andy_body_status, andy_body_desc, andy_spirit_status, andy_spirit_desc,
            andy_account_status, andy_account_desc, andy_relations_status, andy_relations_desc, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            energy=excluded.energy, morning_routine=excluded.morning_routine,
            evening_routine=excluded.evening_routine, water=excluded.water,
            andy_body_status=excluded.andy_body_status, andy_body_desc=excluded.andy_body_desc,
            andy_spirit_status=excluded.andy_spirit_status, andy_spirit_desc=excluded.andy_spirit_desc,
            andy_account_status=excluded.andy_account_status, andy_account_desc=excluded.andy_account_desc,
            andy_relations_status=excluded.andy_relations_status, andy_relations_desc=excluded.andy_relations_desc,
            notes=excluded.notes, updated_at=datetime('now')
    """,
        (
            date,
            energy,
            morning_routine,
            evening_routine,
            water,
            andy_body_status,
            andy_body_desc,
            andy_spirit_status,
            andy_spirit_desc,
            andy_account_status,
            andy_account_desc,
            andy_relations_status,
            andy_relations_desc,
            notes,
        ),
    )
    await db.commit()

    if request.headers.get("HX-Request"):
        return PlainTextResponse("saved")
    return RedirectResponse(f"/daily/{date}", status_code=303)


@router.post("/daily/generate-andy")
async def generate_andy(request: Request, date: str = Form(...)):
    if not valid_date(date):
        return RedirectResponse("/daily", status_code=303)
    from app.services.llm import call_llm, parse_andy_response

    db = await get_db()
    target = date
    target_date = date_module.fromisoformat(target)
    day_name = DAYS_PL[target_date.weekday()]

    # Build context from DB instead of markdown files
    context_parts: list[str] = []

    # 1. Goals context
    areas = await db.execute_fetchall("SELECT * FROM goal_areas ORDER BY display_order")
    goals = await db.execute_fetchall("SELECT * FROM goals ORDER BY area_id, horizon, display_order")
    if goals:
        goals_map: dict[tuple, list] = {}
        for g in goals:
            g = dict(g)
            goals_map.setdefault((g["area_id"], g["horizon"]), []).append(g["content"])
        goal_lines = ["--- Goals ---"]
        for a in areas:
            a = dict(a)
            for horizon in ("1yr", "3yr", "10yr"):
                items = goals_map.get((a["id"], horizon), [])
                if items:
                    goal_lines.append(f"{a['name']} ({horizon}):")
                    for item in items:
                        goal_lines.append(f"  - {item}")
        context_parts.append("\n".join(goal_lines))

    # 2. Current week daily logs
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    week_logs = await db.execute_fetchall(
        "SELECT * FROM daily_logs WHERE date BETWEEN ? AND ? ORDER BY date",
        (monday.isoformat(), sunday.isoformat()),
    )
    if week_logs:
        week_lines = ["--- This Week ---"]
        for row in week_logs:
            r = dict(row)
            energy = r.get("energy", "?")
            week_lines.append(
                f"{r['date']}: energy={energy}, body={r.get('andy_body_desc', '')}, "
                f"spirit={r.get('andy_spirit_desc', '')}, account={r.get('andy_account_desc', '')}, "
                f"relations={r.get('andy_relations_desc', '')}"
            )
        context_parts.append("\n".join(week_lines))

    # 3. Training protocol + recent sessions
    exercises = await db.execute_fetchall("SELECT * FROM training_exercises ORDER BY display_order")
    if exercises:
        train_lines = ["--- Training Protocol ---"]
        for ex in exercises:
            ex = dict(ex)
            train_lines.append(f"- {ex['name']}: {ex['target_sets']}x{ex['target_reps']}")
        recent_sessions = await db.execute_fetchall("SELECT * FROM training_sessions ORDER BY date DESC LIMIT 3")
        if recent_sessions:
            train_lines.append("\nRecent sessions:")
            for s in recent_sessions:
                s = dict(s)
                dur = f" ({s['duration_minutes']} min)" if s["duration_minutes"] else ""
                train_lines.append(f"- {s['date']}{dur}")
        context_parts.append("\n".join(train_lines))

    # 4. plan.md from disk (user-written, not generated)
    if SECOND_BRAIN_PATH:
        plan_path = os.path.join(SECOND_BRAIN_PATH, "plan.md")
        if os.path.isfile(plan_path):
            with open(plan_path, encoding="utf-8") as f:
                context_parts.append(f"--- plan.md ---\n{f.read()[:3000]}")

    system_prompt = (
        "You are a personal daily planner. Based on the user's goals, weekly plan, training protocol, "
        "and current week's data, suggest specific actions for today. "
        "Respond ONLY with valid JSON, no markdown fences. "
        'The JSON must have exactly these keys: "andy_body_desc", "andy_spirit_desc", "andy_account_desc", "andy_relations_desc". '
        "Each value should be a concise task description in English (max 60 chars)."
    )

    user_parts = [f"Date: {target} ({day_name})\n"]
    for part in context_parts:
        user_parts.append(part + "\n")
    user_prompt = "\n".join(user_parts)

    try:
        raw = await call_llm(db, system_prompt, user_prompt)
        data = parse_andy_response(raw)

        await db.execute(
            """
            INSERT INTO daily_logs (date, andy_body_desc, andy_spirit_desc, andy_account_desc, andy_relations_desc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                andy_body_desc=excluded.andy_body_desc, andy_spirit_desc=excluded.andy_spirit_desc,
                andy_account_desc=excluded.andy_account_desc, andy_relations_desc=excluded.andy_relations_desc,
                updated_at=datetime('now')
            """,
            (
                target,
                data.get("andy_body_desc", ""),
                data.get("andy_spirit_desc", ""),
                data.get("andy_account_desc", ""),
                data.get("andy_relations_desc", ""),
            ),
        )
        await db.commit()
    except Exception:
        logger.exception("Failed to generate A.N.D.Y. suggestions")

    redirect_url = f"/daily/{target}"
    if request.headers.get("HX-Request"):
        return Response(status_code=200, headers={"HX-Redirect": redirect_url})
    return RedirectResponse(redirect_url, status_code=303)


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
        return RedirectResponse("/daily", status_code=303)

    def to_float(v: str) -> float | None:
        try:
            return float(v) if v else None
        except ValueError:
            return None

    db = await get_db()
    await db.execute(
        """
        INSERT INTO body_measurements (date, weight, arm, waist, hips, thighs)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            weight=excluded.weight, arm=excluded.arm, waist=excluded.waist,
            hips=excluded.hips, thighs=excluded.thighs
    """,
        (date, to_float(weight), to_float(arm), to_float(waist), to_float(hips), to_float(thighs)),
    )
    await db.commit()
    if request.headers.get("HX-Request"):
        return PlainTextResponse("saved")
    return RedirectResponse(f"/daily/{date}", status_code=303)
