from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app.db import get_db
from app.main import templates
from app.validation import truncate

router = APIRouter()

HORIZONS = [("1yr", "1 Year"), ("3yr", "3 Years"), ("10yr", "10 Years")]


@router.get("/goals", response_class=HTMLResponse)
async def goals_page(request: Request):
    db = await get_db()

    areas = await db.execute_fetchall("SELECT * FROM goal_areas ORDER BY display_order")
    areas = [dict(a) for a in areas]

    goals = await db.execute_fetchall("SELECT * FROM goals ORDER BY area_id, horizon, display_order")
    goals = [dict(g) for g in goals]

    # Group goals by area_id and horizon
    goals_map = {}
    for g in goals:
        key = (g["area_id"], g["horizon"])
        if key not in goals_map:
            goals_map[key] = []
        goals_map[key].append(g)

    return templates.TemplateResponse(
        "goals.html",
        {
            "request": request,
            "areas": areas,
            "horizons": HORIZONS,
            "goals_map": goals_map,
        },
    )


@router.post("/goals/save")
async def save_goal(
    request: Request,
    goal_id: int | None = Form(None),
    area_id: int = Form(...),
    horizon: str = Form(...),
    content: str = Form(...),
    display_order: int = Form(0),
):
    if horizon not in ("1yr", "3yr", "10yr"):
        return RedirectResponse("/goals", status_code=303)
    content = truncate(content, 2000)
    if not content.strip():
        return RedirectResponse("/goals", status_code=303)
    db = await get_db()
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
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/update-inline")
async def update_goal_inline(
    request: Request,
    goal_id: int = Form(...),
    content: str = Form(...),
):
    content = truncate(content, 2000)
    db = await get_db()
    await db.execute(
        "UPDATE goals SET content = ?, updated_at = datetime('now') WHERE id = ?",
        (content.strip(), goal_id),
    )
    await db.commit()
    return PlainTextResponse("saved")


@router.post("/goals/delete")
async def delete_goal(request: Request, goal_id: int = Form(...)):
    db = await get_db()
    await db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    await db.commit()
    return RedirectResponse("/goals", status_code=303)
