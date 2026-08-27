from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormInt, truncate

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
        },
    )


@router.post("/goals/toggle-focus")
async def toggle_focus(request: Request, goal_id: int = Form(...), horizon: str = Form(DEFAULT_HORIZON)):
    """Flip a goal in or out of the current focus set.

    Refuses nothing on count: the page warns above FOCUS_SOFT_LIMIT instead,
    which keeps the guidance without turning a save into a loss.
    """
    db = get_user_db_from_request(request)
    await db.execute("UPDATE goals SET active = 1 - active, updated_at = datetime('now') WHERE id = ?", (goal_id,))
    await db.commit()
    return RedirectResponse(f"/goals?horizon={_safe_horizon(horizon)}", status_code=303)


@router.post("/goals/save")
async def save_goal(
    request: Request,
    goal_id: OptionalFormInt = None,
    area_id: int = Form(...),
    horizon: str = Form(...),
    content: str = Form(...),
    display_order: int = Form(0),
):
    if horizon not in HORIZON_KEYS:
        return RedirectResponse("/goals", status_code=303)
    content = truncate(content, 2000)
    if not content.strip():
        return RedirectResponse("/goals", status_code=303)
    db = get_user_db_from_request(request)
    if goal_id is not None:
        await db.execute(
            "UPDATE goals SET content = ?, display_order = ?, updated_at = datetime('now') WHERE id = ?",
            (content.strip(), display_order, goal_id),
        )
    else:
        await db.execute(
            "INSERT INTO goals (area_id, horizon, content, display_order) VALUES (?, ?, ?, ?)",
            (area_id, horizon, content.strip(), display_order),
        )
    await db.commit()
    # Back to the horizon the goal belongs to, not to the default one.
    return RedirectResponse(f"/goals?horizon={horizon}", status_code=303)


@router.post("/goals/update-inline")
async def update_goal_inline(
    request: Request,
    goal_id: int = Form(...),
    content: str = Form(...),
):
    content = truncate(content, 2000)
    db = get_user_db_from_request(request)
    await db.execute(
        "UPDATE goals SET content = ?, updated_at = datetime('now') WHERE id = ?",
        (content.strip(), goal_id),
    )
    await db.commit()
    return PlainTextResponse("saved")


@router.post("/goals/delete")
async def delete_goal(request: Request, goal_id: int = Form(...), horizon: str = Form(DEFAULT_HORIZON)):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    await db.commit()
    return RedirectResponse(f"/goals?horizon={_safe_horizon(horizon)}", status_code=303)
