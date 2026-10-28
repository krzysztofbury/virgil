"""The public landing page (#168): what Virgil is, and how to get in."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

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
