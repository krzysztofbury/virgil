"""Admin panel — user management."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.central_db import delete_user, get_all_users, update_user
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.main import templates
from app.user_db import delete_user_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _require_admin(request: Request) -> dict:
    """Return user dict if admin, raise 403 otherwise."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request):
    _require_admin(request)
    users = await get_all_users()
    return templates.TemplateResponse(
        "admin_users.html",
        {
            "request": request,
            "users": users,
            "total": len(users),
            "registration_open": REGISTRATION_OPEN,
            "admin_emails": ADMIN_EMAILS,
        },
    )


@router.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: str):
    _require_admin(request)
    await update_user(user_id, is_active=0)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    _require_admin(request)
    await update_user(user_id, is_active=1)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    db_filename = await delete_user(user_id)
    if db_filename:
        delete_user_db(db_filename)
    return RedirectResponse("/admin/users", status_code=303)
