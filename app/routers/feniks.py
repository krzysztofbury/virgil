import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.streak import get_streak, get_week_clean
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormInt, clamp, truncate, valid_date

logger = logging.getLogger(__name__)


async def require_feniks(request: Request):
    if not getattr(request.state, "features", {}).get("no_porn", False):
        raise HTTPException(status_code=303, headers={"Location": "/"})


router = APIRouter(dependencies=[Depends(require_feniks)])


def _past_or_today(value: str) -> str | None:
    """Normalise a form date, or None if it is unparseable or still ahead.

    Normalising matters because date.fromisoformat also accepts compact and
    week forms, which no longer compare correctly as plain text.
    """
    from datetime import date as calendar_date

    try:
        parsed = calendar_date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed <= calendar_date.today() else None


@router.get("/feniks", response_class=HTMLResponse)
async def feniks_page(request: Request):
    edit_date = request.query_params.get("date")

    db = get_user_db_from_request(request)
    today = date.today()

    # Streak (informational) + weekly clean rate (Gola 75%/week, never resets to 0)
    streak_days, _ = await get_streak(db)
    week_clean, week_elapsed, week_pct = await get_week_clean(db)

    # Week strip: Monday..Sunday dots, derived from pmo_events like week_clean is
    monday = today - timedelta(days=today.weekday())
    relapse_rows = await db.execute_fetchall(
        "SELECT DISTINCT date FROM pmo_events WHERE event_type = 'relapse' AND date BETWEEN ? AND ?",
        (monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
    )
    relapse_dates = {r["date"] for r in relapse_rows}
    week_days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        if d > today:
            state = "future"
        elif d.isoformat() in relapse_dates:
            state = "used"
        else:
            state = "clean"
        week_days.append({"date": d.isoformat(), "label": "MTWTFSS"[i], "state": state})

    # Bricks (urges survived) — the module's progress unit
    bricks = await db.execute_fetchall("SELECT * FROM feniks_bricks ORDER BY date DESC, id DESC LIMIT 60")
    bricks = [dict(r) for r in bricks]
    bricks_total = (await db.execute_fetchall("SELECT COUNT(*) AS c FROM feniks_bricks"))[0]["c"]

    # Daily log rows
    daily = await db.execute_fetchall("SELECT * FROM feniks_daily ORDER BY date DESC LIMIT 60")
    daily = [dict(r) for r in daily]

    form_date = edit_date if edit_date and valid_date(edit_date) else today.isoformat()
    form_daily = next((d for d in daily if d["date"] == form_date), None)

    # Unified timeline: bricks surface above the day row they belong to
    timeline_dates = sorted({d["date"] for d in daily} | {b["date"] for b in bricks}, reverse=True)
    daily_by_date = {d["date"]: d for d in daily}
    timeline = [
        {
            "date": dt,
            "day": daily_by_date.get(dt),
            "bricks": [b for b in bricks if b["date"] == dt],
        }
        for dt in timeline_dates[:30]
    ]

    return templates.TemplateResponse(
        "feniks.html",
        {
            "request": request,
            "streak_days": streak_days,
            "week_clean": week_clean,
            "week_elapsed": week_elapsed,
            "week_pct": week_pct,
            "week_days": week_days,
            "bricks_total": bricks_total,
            "timeline": timeline,
            "today": today.isoformat(),
            "form_date": form_date,
            "form_daily": form_daily,
        },
    )


@router.post("/feniks/daily")
async def save_daily(
    request: Request,
    date: str = Form(...),
    used: str = Form(""),
    edging: str = Form(""),
    note: str = Form(""),
    minutes: OptionalFormInt = None,
):
    """The day log: clean / watched, plus minutes, edging and a one-line note.
    Upsert per date.

    used=1 also records a relapse pmo_event for that date (once); correcting the
    day back to used=0 removes only that marker event ('via daily log'), never a
    relapse recorded by another path — so the streak / weekly clean-rate stay
    consistent with the daily log. A historical relapse event without a daily
    row is history, not corruption.
    """
    if not valid_date(date) or used not in ("0", "1"):
        return error_redirect(request, "/feniks", "Choose a valid date and day outcome.")
    used_val = int(used)
    edging_val = 1 if edging == "1" else 0
    minutes_val = clamp(minutes, 0, 1440)
    note = truncate(note, 500)
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO feniks_daily (date, used, minutes, edging, note)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            used=excluded.used, minutes=excluded.minutes,
            edging=excluded.edging, note=excluded.note
        """,
        (date, used_val, minutes_val, edging_val, note),
    )
    if used_val:
        await db.execute(
            """
            INSERT INTO pmo_events (date, event_type, notes)
            SELECT ?, 'relapse', 'via daily log'
            WHERE NOT EXISTS (SELECT 1 FROM pmo_events WHERE date = ? AND event_type = 'relapse')
            """,
            (date, date),
        )
    else:
        await db.execute(
            "DELETE FROM pmo_events WHERE date = ? AND event_type = 'relapse' AND notes = 'via daily log'",
            (date,),
        )
    await db.commit()
    return success_redirect(
        request,
        f"/feniks?date={date}",
        "Day saved.",
        clear_draft=f"feniks-daily:{date}",
    )


@router.post("/feniks/bricks")
async def save_brick(
    request: Request,
    date: str = Form(...),
    hook: str = Form(""),
    story: str = Form(""),
    craving: OptionalFormInt = None,
):
    """A brick = one urge survived, in Gola's structure. The hook (hak
    pamięciowy) is what makes it retrievable under pressure — required."""
    if not hook.strip():
        return error_redirect(request, "/feniks", "Name the brick before saving it.")
    # The page can be opened on an earlier day, so the brick carries its own
    # date instead of always landing on today. A future one is always a typo:
    # the urge has not happened yet.
    entry_date = _past_or_today(date)
    if entry_date is None:
        return error_redirect(request, "/feniks", "Give the brick a date of today or earlier.")
    date = entry_date
    hook = truncate(hook.strip(), 200)
    story = truncate(story, 2000)
    craving_val = clamp(craving, 0, 10)
    db = get_user_db_from_request(request)
    await db.execute(
        "INSERT INTO feniks_bricks (date, hook, craving, story) VALUES (?, ?, ?, ?)",
        (date, hook, craving_val, story),
    )
    await db.commit()
    return success_redirect(
        request,
        f"/feniks?date={date}",
        "Brick added.",
        clear_draft=f"feniks-brick:{date}",
    )
