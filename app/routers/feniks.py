import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.services.streak import get_streak
from app.user_db import get_user_db_from_request
from app.validation import truncate, valid_date

logger = logging.getLogger(__name__)


async def require_feniks(request: Request):
    if not getattr(request.state, "features", {}).get("no_porn", False):
        raise HTTPException(status_code=303, headers={"Location": "/"})


router = APIRouter(dependencies=[Depends(require_feniks)])


@router.get("/feniks", response_class=HTMLResponse)
@router.get("/feniks/{tab}", response_class=HTMLResponse)
async def feniks_page(request: Request, tab: str = "journal"):
    edit_date = request.query_params.get("date")

    db = get_user_db_from_request(request)
    today = date.today()

    # Feniks config
    conf = await db.execute_fetchall("SELECT * FROM feniks_config WHERE id = 1")
    config = dict(conf[0]) if conf else None

    # Streak
    streak_days, _ = await get_streak(db)
    progress = min(100, round(streak_days / config["target_days"] * 100)) if config else 0

    # Journal entries
    journal = await db.execute_fetchall("SELECT * FROM feniks_journal ORDER BY date DESC LIMIT 30")
    journal = [dict(r) for r in journal]

    # Journal entry to edit (today or specific date via ?date= param)
    form_date = edit_date if edit_date else today.isoformat()
    today_journal = await db.execute_fetchall("SELECT * FROM feniks_journal WHERE date = ?", (form_date,))
    today_journal = dict(today_journal[0]) if today_journal else None

    # Pleasures
    pleasures = await db.execute_fetchall("SELECT * FROM feniks_pleasures ORDER BY date DESC LIMIT 30")
    pleasures = [dict(r) for r in pleasures]

    today_pleasures = await db.execute_fetchall("SELECT * FROM feniks_pleasures WHERE date = ?", (today.isoformat(),))
    today_pleasures = dict(today_pleasures[0]) if today_pleasures else None

    # Milestones
    milestones = await db.execute_fetchall("SELECT * FROM feniks_milestones ORDER BY day_number")
    milestones = [dict(r) for r in milestones]

    # Group by week
    weeks = {}
    for m in milestones:
        w = m["week_number"]
        if w not in weeks:
            weeks[w] = []
        weeks[w].append(m)

    # Progress graph: build daily streak timeline from pmo_events
    # Start from feniks_config start_date, show streak building up, resets on relapse
    relapses = await db.execute_fetchall("SELECT date FROM pmo_events WHERE event_type = 'relapse' ORDER BY date")
    relapse_dates = [r["date"] for r in relapses]

    progress_chart = {"labels": [], "streak_values": [], "relapses": []}
    if config:
        start = date.fromisoformat(config["start_date"])
        end = today
        current_streak = 0
        relapse_set = set(relapse_dates)
        d = start
        while d <= end:
            ds = d.isoformat()
            progress_chart["labels"].append(ds[5:])
            if ds in relapse_set:
                progress_chart["relapses"].append(len(progress_chart["labels"]) - 1)
                current_streak = 0
            else:
                current_streak += 1
            progress_chart["streak_values"].append(current_streak)
            d += timedelta(days=1)

    return templates.TemplateResponse(
        "feniks.html",
        {
            "request": request,
            "tab": tab,
            "config": config,
            "streak_days": streak_days,
            "progress": progress,
            "journal": journal,
            "today_journal": today_journal,
            "pleasures": pleasures,
            "today_pleasures": today_pleasures,
            "weeks": weeks,
            "today": today.isoformat(),
            "form_date": form_date,
            "progress_chart": progress_chart,
        },
    )


@router.post("/feniks/journal")
async def save_journal(
    request: Request,
    date: str = Form(...),
    emotions: str = Form(""),
    triggers: str = Form(""),
    thoughts: str = Form(""),
    desired_feelings: str = Form(""),
    coping_strategies: str = Form(""),
):
    if not valid_date(date):
        return RedirectResponse("/feniks/journal", status_code=303)
    emotions = truncate(emotions, 2000)
    triggers = truncate(triggers, 2000)
    thoughts = truncate(thoughts, 2000)
    desired_feelings = truncate(desired_feelings, 2000)
    coping_strategies = truncate(coping_strategies, 2000)
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO feniks_journal (date, emotions, triggers, thoughts, desired_feelings, coping_strategies)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            emotions=excluded.emotions, triggers=excluded.triggers, thoughts=excluded.thoughts,
            desired_feelings=excluded.desired_feelings, coping_strategies=excluded.coping_strategies
    """,
        (date, emotions, triggers, thoughts, desired_feelings, coping_strategies),
    )
    await db.commit()
    return RedirectResponse("/feniks/journal", status_code=303)


@router.post("/feniks/pleasures")
async def save_pleasures(
    request: Request,
    date: str = Form(...),
    pleasure_1: str = Form(""),
    pleasure_2: str = Form(""),
):
    if not valid_date(date):
        return RedirectResponse("/feniks/pleasures", status_code=303)
    pleasure_1 = truncate(pleasure_1, 500)
    pleasure_2 = truncate(pleasure_2, 500)
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO feniks_pleasures (date, pleasure_1, pleasure_2)
        VALUES (?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            pleasure_1=excluded.pleasure_1, pleasure_2=excluded.pleasure_2
    """,
        (date, pleasure_1, pleasure_2),
    )
    await db.commit()
    return RedirectResponse("/feniks/pleasures", status_code=303)


@router.post("/feniks/milestone")
async def toggle_milestone(
    request: Request,
    day_number: int = Form(...),
):
    db = get_user_db_from_request(request)
    row = await db.execute_fetchall("SELECT * FROM feniks_milestones WHERE day_number = ?", (day_number,))
    if row:
        old_val = row[0]["completed"]
        new_val = 0 if old_val else 1
        completed_at = date.today().isoformat() if new_val else None
        await db.execute(
            "UPDATE feniks_milestones SET completed = ?, completed_at = ? WHERE day_number = ?",
            (new_val, completed_at, day_number),
        )
        await db.commit()

    # HTMX: return just the updated button
    if request.headers.get("hx-request"):
        m = dict((await db.execute_fetchall("SELECT * FROM feniks_milestones WHERE day_number = ?", (day_number,)))[0])
        checked = m["completed"]
        icon = '<i data-lucide="check" style="width:14px;height:14px;"></i>' if checked else ""
        cls = "active-done" if checked else "active-pending"
        html = (
            f'<button type="submit" class="toggle-btn {cls}" style="width:32px;height:32px;"'
            f' hx-post="/feniks/milestone" hx-target="closest form" hx-swap="innerHTML"'
            f" hx-vals='{{\"day_number\": {day_number}}}'>"
            f"{icon}</button>"
        )
        return HTMLResponse(html)

    return RedirectResponse("/feniks/milestones", status_code=303)


@router.post("/feniks/relapse")
async def log_relapse(
    request: Request,
    date: str = Form(...),
    notes: str = Form(""),
):
    if not valid_date(date):
        return RedirectResponse("/feniks", status_code=303)
    notes = truncate(notes, 2000)
    db = get_user_db_from_request(request)
    await db.execute("INSERT INTO pmo_events (date, event_type, notes) VALUES (?, 'relapse', ?)", (date, notes))
    await db.commit()
    return RedirectResponse("/feniks", status_code=303)
