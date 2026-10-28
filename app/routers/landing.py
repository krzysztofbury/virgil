"""The public landing page (#168): what Virgil is, and how to get in."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import BASE_URL
from app.i18n import current_language
from app.main import templates
from app.server_settings import current

router = APIRouter()


@router.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    from app.routers.auth import registration_allowed

    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "request": request,
            "contact_email": current.CONTACT_EMAIL,
            "registration_open": await registration_allowed(),
        },
    )


def _legal_context(request: Request) -> dict:
    return {
        "request": request,
        "contact_email": current.CONTACT_EMAIL,
        "operator_name": current.OPERATOR_NAME,
        "operator_address": current.OPERATOR_ADDRESS,
        "operator_tax_id": current.OPERATOR_TAX_ID,
        "base_url": BASE_URL,
    }


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, f"legal/privacy_{current_language()}.html", _legal_context(request))


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, f"legal/terms_{current_language()}.html", _legal_context(request))
