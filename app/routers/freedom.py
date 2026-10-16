"""Freedom: the habits a person is breaking free from (app/services/habits.py).

One page for all of them: a Today list to check each habit off at a glance,
then one habit in detail - its day log, the urges survived (bricks), its week
and its settings. /feniks, the module's old address, redirects here.
"""

import logging
from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services import habits as habit_service
from app.services.freedom_log import (
    FreedomWriteError,
    add_brick,
    delete_brick,
    habit_overview,
    past_or_today,
    save_day,
    update_brick,
)
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormInt

logger = logging.getLogger(__name__)


async def require_freedom(request: Request):
    if not getattr(request.state, "features", {}).get("freedom", False):
        raise HTTPException(status_code=303, headers={"Location": "/"})


router = APIRouter(dependencies=[Depends(require_freedom)])


def _page(slug: str | None = None, day: str | None = None) -> str:
    query = {key: value for key, value in (("habit", slug), ("date", day)) if value}
    return "/freedom" + (f"?{urlencode(query)}" if query else "")


@router.get("/feniks")
async def old_address(request: Request):
    query = request.url.query
    return RedirectResponse("/freedom" + (f"?{query}" if query else ""), status_code=308)


@router.get("/freedom", response_class=HTMLResponse)
async def freedom_page(request: Request):
    db = get_user_db_from_request(request)
    today = date.today()
    tracked = await habit_service.list_habits(db)
    everything = await habit_service.list_habits(db, tracked_only=False)
    tracked_slugs = {h["slug"] for h in tracked}
    # What can still be added: the catalog plus custom habits no longer tracked.
    catalog = [p for p in habit_service.PRESETS if p["slug"] not in tracked_slugs]
    catalog += [h for h in everything if not h["tracked"] and h["slug"] not in habit_service.PRESETS_BY_SLUG]
    context = {"request": request, "today": today.isoformat(), "habits": tracked, "catalog": catalog}
    if not tracked:
        return templates.TemplateResponse(request, "freedom.html", context)

    requested = request.query_params.get("habit")
    habit = next((h for h in tracked if h["slug"] == requested), tracked[0])
    overviews = [await habit_overview(db, h) for h in tracked]
    selected = next(o for o in overviews if o["habit"] == habit["slug"])

    # Week strip: Monday..Sunday dots, from relapse markers like the clean rate
    monday = today - timedelta(days=today.weekday())
    relapse_rows = await db.execute_fetchall(
        "SELECT DISTINCT date FROM pmo_events WHERE habit_id = ? AND event_type = 'relapse' AND date BETWEEN ? AND ?",
        (habit["id"], monday.isoformat(), (monday + timedelta(days=6)).isoformat()),
    )
    relapse_dates = {r["date"] for r in relapse_rows}
    week_days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        state = "future" if d > today else "used" if d.isoformat() in relapse_dates else "clean"
        week_days.append({"date": d.isoformat(), "label": "MTWTFSS"[i], "state": state})

    bricks = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM feniks_bricks WHERE habit_id = ? ORDER BY date DESC, id DESC LIMIT 60", (habit["id"],)
        )
    ]
    daily = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM feniks_daily WHERE habit_id = ? ORDER BY date DESC LIMIT 60", (habit["id"],)
        )
    ]

    edit_date = request.query_params.get("date")
    form_date = (past_or_today(edit_date) if edit_date else None) or today.isoformat()
    form_daily = next((d for d in daily if d["date"] == form_date), None)
    if form_daily is None:
        rows = await db.execute_fetchall(
            "SELECT * FROM feniks_daily WHERE habit_id = ? AND date = ?", (habit["id"], form_date)
        )
        form_daily = dict(rows[0]) if rows else None

    # One timeline: bricks surface above the day row they belong to
    timeline_dates = sorted({d["date"] for d in daily} | {b["date"] for b in bricks}, reverse=True)
    daily_by_date = {d["date"]: d for d in daily}
    timeline = [
        {"date": dt, "day": daily_by_date.get(dt), "bricks": [b for b in bricks if b["date"] == dt]}
        for dt in timeline_dates[:30]
    ]

    context.update(
        {
            "habit": habit,
            "overviews": overviews,
            "selected": selected,
            "week_days": week_days,
            "weekly_target": habit_service.WEEKLY_TARGET_PCT,
            "timeline": timeline,
            "form_date": form_date,
            "form_daily": form_daily,
        }
    )
    return templates.TemplateResponse(request, "freedom.html", context)


async def _habit_or_redirect(request: Request, slug: str):
    habit = await habit_service.get_habit(get_user_db_from_request(request), slug)
    if habit is None:
        return None, error_redirect(request, "/freedom", "That habit was not found.")
    return habit, None


@router.post("/freedom/habits")
async def add_habit(request: Request, slug: str = Form(""), name: str = Form("")):
    """Start tracking a catalog habit (slug) or one named in the person's own words."""
    db = get_user_db_from_request(request)
    try:
        habit = await habit_service.track(db, slug) if slug else await habit_service.add_custom(db, name)
    except (LookupError, habit_service.HabitError) as exc:
        return error_redirect(request, "/freedom", str(exc))
    return success_redirect(request, _page(habit["slug"]), f"Tracking {habit['name']}.")


@router.post("/freedom/{slug}/untrack")
async def untrack_habit(request: Request, slug: str):
    habit, refusal = await _habit_or_redirect(request, slug)
    if refusal:
        return refusal
    await habit_service.untrack(get_user_db_from_request(request), slug)
    return success_redirect(request, "/freedom", f"Stopped tracking {habit['name']}. Its history is kept.")


@router.post("/freedom/{slug}/settings")
async def save_habit_settings(
    request: Request,
    slug: str,
    name: str = Form(...),
    slip_label: str = Form(...),
    slip_definition: str = Form(""),
    amount_unit: str = Form(""),
    near_miss_label: str = Form(""),
    start_date: str = Form(...),
    big_why: str = Form(""),
    target_days: OptionalFormInt = None,
    slip_threshold: OptionalFormInt = None,
):
    changes = {
        "name": name,
        "slip_label": slip_label,
        "slip_definition": slip_definition,
        "amount_unit": amount_unit,
        "slip_threshold": slip_threshold,
        "near_miss_label": near_miss_label,
        "start_date": start_date,
        "big_why": big_why,
    }
    if target_days is not None:
        changes["target_days"] = target_days
    try:
        habit = await habit_service.update_habit(get_user_db_from_request(request), slug, changes)
    except LookupError as exc:
        return error_redirect(request, "/freedom", str(exc))
    except habit_service.HabitError as exc:
        return error_redirect(request, _page(slug), str(exc))
    return success_redirect(request, _page(habit["slug"]), f"{habit['name']} settings saved.")


@router.post("/freedom/{slug}/daily")
async def save_daily(
    request: Request,
    slug: str,
    date: str = Form(...),
    slipped: str = Form(""),
    near_miss: str = Form(""),
    note: str = Form(""),
    view: str = Form(""),
    amount: OptionalFormInt = None,
):
    """One habit's day: clean or slipped, with the amount and near miss the habit asks for.

    `view` is the habit on screen when the Today list checks another one off, so a
    quick tap leaves the page where it was.

    A slip also records that habit's relapse marker for the date (once);
    correcting the day back to clean removes only that marker.
    """
    habit, refusal = await _habit_or_redirect(request, slug)
    if refusal:
        return refusal
    entry_date = past_or_today(date)
    if entry_date is None or slipped not in ("0", "1"):
        return error_redirect(request, _page(slug), "Choose a valid date and day outcome.")
    try:
        await save_day(
            get_user_db_from_request(request),
            habit,
            entry_date,
            slipped=slipped == "1",
            amount=amount,
            near_miss=near_miss == "1",
            note=note,
        )
    except FreedomWriteError as exc:
        return error_redirect(request, _page(slug, entry_date), str(exc))
    return success_redirect(
        request,
        _page(view) if view else _page(slug, entry_date),
        f"{habit['name']}: day saved.",
        clear_draft=f"freedom-daily:{slug}:{entry_date}",
    )


@router.post("/freedom/{slug}/bricks")
async def save_brick(
    request: Request,
    slug: str,
    date: str = Form(...),
    hook: str = Form(""),
    story: str = Form(""),
    craving: OptionalFormInt = None,
):
    """A brick = one urge survived, in Gola's structure. The hook (hak
    pamięciowy) is what makes it retrievable under pressure - required."""
    habit, refusal = await _habit_or_redirect(request, slug)
    if refusal:
        return refusal
    try:
        brick = await add_brick(get_user_db_from_request(request), habit, date, hook=hook, craving=craving, story=story)
    except FreedomWriteError as exc:
        return error_redirect(request, _page(slug), str(exc))
    return success_redirect(
        request, _page(slug, brick["date"]), "Brick added.", clear_draft=f"freedom-brick:{slug}:{brick['date']}"
    )


@router.post("/freedom/bricks/{brick_id}/edit")
async def edit_brick(
    request: Request,
    brick_id: int,
    date: str = Form(...),
    hook: str = Form(""),
    story: str = Form(""),
    craving: OptionalFormInt = None,
):
    db = get_user_db_from_request(request)
    try:
        brick = await update_brick(db, brick_id, {"date": date, "hook": hook, "craving": craving, "story": story})
    except FreedomWriteError as exc:
        return error_redirect(request, _page(day=past_or_today(date)), str(exc))
    except LookupError as exc:
        return error_redirect(request, "/freedom", str(exc))
    return success_redirect(request, _page(brick["habit"], brick["date"]), "Brick updated.")


@router.post("/freedom/bricks/{brick_id}/delete")
async def remove_brick(request: Request, brick_id: int):
    try:
        removed = await delete_brick(get_user_db_from_request(request), brick_id)
    except LookupError as exc:
        return error_redirect(request, "/freedom", str(exc))
    return success_redirect(request, _page(removed["habit"], removed["date"]), "Brick removed from your history.")
