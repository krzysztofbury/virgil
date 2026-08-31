from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.goal_data import (
    GOAL_STATUSES,
    REP_PERIODS,
    GoalDataError,
    create_goal,
    create_rep,
    delete_goal_record,
    delete_rep_record,
    transition_rep,
    update_goal,
    update_rep,
)
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormInt

router = APIRouter()

HORIZONS = [("1yr", "1 Year"), ("3yr", "3 Years"), ("10yr", "10 Years")]
HORIZON_KEYS = [key for key, _ in HORIZONS]
DEFAULT_HORIZON = "1yr"

# Advisory, not enforced. Three is about what a person can hold at once; above
# that the page says so and still saves the fourth. A cap that refuses a write is
# a worse failure than a crowded focus list.
FOCUS_SOFT_LIMIT = 3


def _safe_horizon(horizon: str) -> str:
    """An unknown horizon falls back rather than rendering an empty page."""
    return horizon if horizon in HORIZON_KEYS else DEFAULT_HORIZON


@router.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request, horizon: str = DEFAULT_HORIZON):
    db = get_user_db_from_request(request)
    horizon = _safe_horizon(horizon)

    areas = [dict(a) for a in await db.execute_fetchall("SELECT * FROM goal_areas ORDER BY display_order")]

    # The focus set spans every area and horizon. "Now" is not a horizon of its
    # own: it is whichever goals the user starred, wherever they live.
    focus = [
        dict(g)
        for g in await db.execute_fetchall(
            "SELECT g.*, a.name AS area_name, a.icon AS area_icon FROM goals g "
            "JOIN goal_areas a ON a.id = g.area_id WHERE g.active = 1 "
            "ORDER BY g.updated_at DESC, g.id DESC"
        )
    ]

    # One horizon at a time. All three at once is what made this page 3356 px of
    # identical inputs.
    rows = await db.execute_fetchall(
        "SELECT * FROM goals WHERE horizon = ? ORDER BY area_id, display_order", (horizon,)
    )
    goals_by_area: dict[int, list[dict]] = {}
    for g in rows:
        goals_by_area.setdefault(g["area_id"], []).append(dict(g))

    pending_reps = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT r.*, g.content AS goal_content FROM goal_reps r JOIN goals g ON g.id = r.goal_id "
            "WHERE r.status = 'pending' ORDER BY r.due_date, r.id LIMIT 100"
        )
    ]
    rep_history = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT r.*, g.content AS goal_content FROM goal_reps r JOIN goals g ON g.id = r.goal_id "
            "WHERE r.status != 'pending' ORDER BY r.updated_at DESC, r.id DESC LIMIT 30"
        )
    ]

    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "areas": areas,
            "horizons": HORIZONS,
            "horizon": horizon,
            "goals_by_area": goals_by_area,
            "areas_with_goals": [a for a in areas if goals_by_area.get(a["id"])],
            "areas_without_goals": [a for a in areas if not goals_by_area.get(a["id"])],
            "focus": focus,
            "focus_limit": FOCUS_SOFT_LIMIT,
            "focus_over": len(focus) > FOCUS_SOFT_LIMIT,
            "goal_statuses": GOAL_STATUSES,
            "rep_periods": REP_PERIODS,
            "pending_reps": pending_reps,
            "rep_history": rep_history,
        },
    )


@router.post("/goals/toggle-focus")
async def toggle_focus(request: Request, goal_id: int = Form(...), horizon: str = Form(DEFAULT_HORIZON)):
    """Flip a goal in or out of the current focus set.

    Refuses nothing on count: the page warns above FOCUS_SOFT_LIMIT instead,
    which keeps the guidance without turning a save into a loss.
    """
    destination = f"/goals?horizon={_safe_horizon(horizon)}"
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT status FROM goals WHERE id = ?", (goal_id,))
    if not rows:
        return error_redirect(request, destination, "Goal was not found.")
    if rows[0]["status"] != "active":
        return error_redirect(request, destination, "Only active goals can be added to current focus.")
    cursor = await db.execute(
        "UPDATE goals SET active = 1 - active, updated_at = datetime('now') WHERE id = ? AND status = 'active'",
        (goal_id,),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, destination, "Goal focus changed concurrently. Try again.")
    return success_redirect(request, destination, "Goal focus updated.")


@router.post("/goals/save")
async def save_goal(
    request: Request,
    area_id: int = Form(...),
    horizon: str = Form(...),
    content: str = Form(...),
    display_order: int = Form(0),
    status: str = Form("active"),
    start_date: str = Form(""),
    end_date: str = Form(""),
):
    db = get_user_db_from_request(request)
    try:
        await create_goal(
            db,
            area_id=area_id,
            horizon=horizon,
            content=content,
            display_order=display_order,
            status=status,
            start_date=start_date,
            end_date=end_date,
        )
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, f"/goals?horizon={_safe_horizon(horizon)}", exc.message)
    await db.commit()
    # Back to the horizon the goal belongs to, not to the default one.
    return success_redirect(request, f"/goals?horizon={horizon}", "Goal saved.")


@router.post("/goals/details")
async def save_goal_details(
    request: Request,
    goal_id: int = Form(...),
    horizon: str = Form(...),
    status: str = Form("active"),
    start_date: str = Form(""),
    end_date: str = Form(""),
):
    db = get_user_db_from_request(request)
    try:
        await update_goal(
            db,
            goal_id,
            {"status": status, "start_date": start_date, "end_date": end_date},
        )
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, f"/goals?horizon={_safe_horizon(horizon)}", exc.message)
    await db.commit()
    return success_redirect(request, f"/goals?horizon={_safe_horizon(horizon)}", "Goal details saved.")


@router.post("/goals/update-inline")
async def update_goal_inline(
    request: Request,
    goal_id: int = Form(...),
    content: str = Form(...),
):
    db = get_user_db_from_request(request)
    try:
        await update_goal(db, goal_id, {"content": content})
    except GoalDataError as exc:
        await db.rollback()
        return PlainTextResponse(exc.message, status_code=exc.status_code)
    await db.commit()
    return PlainTextResponse("saved")


@router.post("/goals/delete")
async def delete_goal(request: Request, goal_id: int = Form(...), horizon: str = Form(DEFAULT_HORIZON)):
    destination = f"/goals?horizon={_safe_horizon(horizon)}"
    db = get_user_db_from_request(request)
    try:
        await delete_goal_record(db, goal_id)
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, destination, exc.message)
    await db.commit()
    return success_redirect(request, destination, "Goal deleted.")


@router.post("/goals/reps/save")
async def save_goal_rep(
    request: Request,
    goal_id: int = Form(...),
    content: str = Form(...),
    period: str = Form("month"),
    due_date: str = Form(...),
    notes: str = Form(""),
    rep_id: OptionalFormInt = None,
):
    db = get_user_db_from_request(request)
    try:
        if rep_id is None:
            await create_rep(
                db,
                goal_id=goal_id,
                content=content,
                period=period,
                due_date=due_date,
                notes=notes,
            )
        else:
            await update_rep(
                db,
                rep_id,
                {"goal_id": goal_id, "content": content, "period": period, "due_date": due_date, "notes": notes},
            )
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, "/goals", exc.message)
    await db.commit()
    return success_redirect(request, "/goals", "Execution rep saved.")


@router.post("/goals/reps/{rep_id}/transition")
async def transition_goal_rep(
    request: Request,
    rep_id: int,
    action: str = Form(...),
    due_date: str = Form(""),
    period: str = Form(""),
):
    db = get_user_db_from_request(request)
    try:
        await transition_rep(db, rep_id, action, due_date=due_date or None, period=period or None)
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, "/goals", exc.message)
    await db.commit()
    return success_redirect(request, "/goals", f"Execution rep {action}d.")


@router.post("/goals/reps/{rep_id}/delete")
async def delete_goal_rep(request: Request, rep_id: int):
    db = get_user_db_from_request(request)
    try:
        await delete_rep_record(db, rep_id)
    except GoalDataError as exc:
        await db.rollback()
        return error_redirect(request, "/goals", exc.message)
    await db.commit()
    return success_redirect(request, "/goals", "Execution rep removed.")
