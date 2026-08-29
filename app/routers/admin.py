"""Admin panel — user management."""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.central_db import delete_user, get_all_users, get_user_by_id, update_user
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.user_db import delete_user_db
from app.validation import valid_uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _validate_user_id(user_id: str) -> None:
    if not valid_uuid(user_id):
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
        return error_redirect(request, "/admin/users", "Your account cannot be disabled from the admin panel.")
    if not await get_user_by_id(user_id):
        return error_redirect(request, "/admin/users", "User account was not found.")
    await update_user(user_id, is_active=0)
    return success_redirect(request, "/admin/users", "User account disabled.")


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    _require_admin(request)
    _validate_user_id(user_id)
    if not await get_user_by_id(user_id):
        return error_redirect(request, "/admin/users", "User account was not found.")
    await update_user(user_id, is_active=1)
    return success_redirect(request, "/admin/users", "User account enabled.")


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", "Your account cannot be deleted from the admin panel.")
    db_filename = await delete_user(user_id)
    if not db_filename:
        return error_redirect(request, "/admin/users", "User account was not found.")
    delete_user_db(db_filename)
    return success_redirect(request, "/admin/users", "User account deleted.")
