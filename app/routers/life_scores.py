from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app.feedback import error_redirect, success_redirect
from app.i18n import _, localized
from app.services.life_scores import LifeScoreError, upsert_life_scores
from app.user_db import get_user_db_from_request
from app.validation import OptionalFormFloat, OptionalFormInt, valid_date

router = APIRouter()

HOME = "/#life-scores"


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
    diagnostic: str = Form(""),
    priorities: str = Form(""),
):
    date = valid_date(date)
    if not date:
        return error_redirect(request, HOME, _("Choose a valid life-score date."))
    filled = {
        "planning": planning,
        "spirituality": spirituality,
        "health": health,
        "work": work,
        "social": social,
        "growth": growth,
        "relaxation": relaxation,
        "family": family,
        "power_level": power_level,
        "diagnostic": diagnostic.strip() or None,
        "priorities": priorities.strip() or None,
    }
    # A blank field keeps what that date holds, so the form can add to an assessment.
    changes = {field: value for field, value in filled.items() if value is not None}
    try:
        await upsert_life_scores(get_user_db_from_request(request), date, changes)
    except LifeScoreError as exc:
        return error_redirect(request, HOME, localized(exc))
    return success_redirect(request, HOME, _("Life scores saved."))
