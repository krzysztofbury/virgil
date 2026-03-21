"""Admin panel — user management."""

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.central_db import delete_user, get_all_users, update_user
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.main import templates
from app.user_db import delete_user_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_user_id(user_id: str) -> None:
    if not _UUID_RE.match(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")


def _require_admin(request: Request) -> dict:
    """Return user dict if admin, raise 403 otherwise."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "admin":
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
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    await update_user(user_id, is_active=0)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    _require_admin(request)
    _validate_user_id(user_id)
    await update_user(user_id, is_active=1)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    db_filename = await delete_user(user_id)
    if db_filename:
        delete_user_db(db_filename)
    return RedirectResponse("/admin/users", status_code=303)
