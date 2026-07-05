from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.db import LIFE_AREA_LABELS, LIFE_AREAS
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormFloat, OptionalFormInt, clamp, truncate, valid_date

router = APIRouter()

AREAS = LIFE_AREAS
AREA_LABELS = LIFE_AREA_LABELS


@router.get("/life-scores")
async def life_scores_page():
    return RedirectResponse("/", status_code=301)


@router.post("/life-scores/save")
async def save_life_score(
    request: Request,
    date: str = Form(...),
    planning: OptionalFormInt = None,
    spirituality: OptionalFormInt = None,
    health: OptionalFormInt = None,
    work: OptionalFormInt = None,
    social: OptionalFormInt = None,
    growth: OptionalFormInt = None,
    relaxation: OptionalFormInt = None,
    family: OptionalFormInt = None,
    power_level: OptionalFormFloat = None,
    weight: OptionalFormFloat = None,
    waist: OptionalFormFloat = None,
    pmo_status: str = Form(""),
    energy_avg: OptionalFormFloat = None,
    linkedin_followers: OptionalFormInt = None,
    youtube_views: OptionalFormInt = None,
    revenue: OptionalFormFloat = None,
    diagnostic: str = Form(""),
    priorities: str = Form(""),
):
    if not valid_date(date):
        return RedirectResponse("/", status_code=303)
    # Clamp area scores to 1-10 range
    planning = clamp(planning, 1, 10)
    spirituality = clamp(spirituality, 1, 10)
    health = clamp(health, 1, 10)
    work = clamp(work, 1, 10)
    social = clamp(social, 1, 10)
    growth = clamp(growth, 1, 10)
    relaxation = clamp(relaxation, 1, 10)
    family = clamp(family, 1, 10)
    pmo_status = truncate(pmo_status, 200)
    diagnostic = truncate(diagnostic, 2000)
    priorities = truncate(priorities, 2000)
    db = get_user_db_from_request(request)
    await db.execute(
        """
        INSERT INTO life_scores (date, planning, spirituality, health, work, social,
            growth, relaxation, family, power_level, weight, waist, pmo_status, energy_avg,
            linkedin_followers, youtube_views, revenue, diagnostic, priorities)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            planning=excluded.planning, spirituality=excluded.spirituality,
            health=excluded.health, work=excluded.work, social=excluded.social,
            growth=excluded.growth, relaxation=excluded.relaxation, family=excluded.family,
            power_level=excluded.power_level, weight=excluded.weight, waist=excluded.waist,
            pmo_status=excluded.pmo_status, energy_avg=excluded.energy_avg,
            linkedin_followers=excluded.linkedin_followers, youtube_views=excluded.youtube_views,
            revenue=excluded.revenue, diagnostic=excluded.diagnostic, priorities=excluded.priorities
    """,
        (
            date,
            planning,
            spirituality,
            health,
            work,
            social,
            growth,
            relaxation,
            family,
            power_level,
            weight,
            waist,
            pmo_status,
            energy_avg,
            linkedin_followers,
            youtube_views,
            revenue,
            diagnostic,
            priorities,
        ),
    )
    await db.commit()
    return RedirectResponse("/", status_code=303)
