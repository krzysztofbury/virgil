import logging
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.services.streak import get_streak, get_week_clean
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

    # Streak (big number, consecutive) + weekly clean rate (Gola 75%/week, never resets to 0)
    streak_days, _ = await get_streak(db)
    week_clean, week_elapsed, week_pct = await get_week_clean(db)

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

    return templates.TemplateResponse(
        "feniks.html",
        {
            "request": request,
            "tab": tab,
            "config": config,
            "streak_days": streak_days,
            "week_clean": week_clean,
            "week_elapsed": week_elapsed,
            "week_pct": week_pct,
            "journal": journal,
            "today_journal": today_journal,
            "pleasures": pleasures,
            "today_pleasures": today_pleasures,
            "today": today.isoformat(),
            "form_date": form_date,
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
