"""Admin panel — user management."""

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.central_db import (
    INVITE_TTL_DAYS,
    RESET_TTL_MINUTES,
    create_invite,
    create_password_reset,
    delete_user_if_unsubscribed,
    disable_user_if_unsubscribed,
    get_all_users,
    get_user_by_id,
    list_auth_events,
    list_open_invites,
    record_auth_event,
    revoke_invite,
    revoke_invites_created_by,
    update_user,
)
from app.config import ADMIN_EMAILS, BASE_URL
from app.feedback import error_redirect, success_redirect
from app.i18n import _, current_language, normalize_language
from app.main import templates
from app.rate_limit import client_ip
from app.server_settings import current
from app.services import mailer
from app.services.backup import delete_user_backups
from app.user_db import delete_user_db
from app.validation import valid_email, valid_uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _validate_user_id(user_id: str) -> None:
    if not valid_uuid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")


async def _event(request: Request, event: str, user_id: str | None, admin: dict, detail: str = "") -> None:
    by = f"by {admin['email']}"
    await record_auth_event(event, user_id=user_id, ip=client_ip(request), detail=f"{detail}, {by}" if detail else by)


async def _protected(user_id: str) -> str | None:
    """The email of an account VIRGIL_ADMIN_EMAILS keeps an admin, or None.

    Those are the operator's break-glass accounts: no admin may disable,
    demote or delete them from the panel."""
    user = await get_user_by_id(user_id)
    return user["email"] if user and user["email"] in ADMIN_EMAILS else None


def _require_admin(request: Request) -> dict:
    """Return user dict if admin, raise 403 otherwise."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def _users_page(request: Request, **extra):
    users = await get_all_users()
    response = templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "request": request,
            "users": users,
            "total": len(users),
            "registration_open": current.REGISTRATION_OPEN,
            "admin_emails": ADMIN_EMAILS,
            "events": await list_auth_events(),
            "invites": await list_open_invites(),
            "invite_days": INVITE_TTL_DAYS,
            "reset_minutes": RESET_TTL_MINUTES,
            **extra,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request):
    _require_admin(request)
    return await _users_page(request)


@router.post("/invites", response_class=HTMLResponse)
async def create_invite_route(
    request: Request,
    email: str = Form(""),
    role: str = Form("user"),
    allow_shared_llm: str = Form(""),
    language: str = Form(""),
):
    """Shows the link once, on this response. Only its hash is stored, so it cannot be shown again.

    With mail on, an invite made for an address is also emailed there (#168), in
    the language the admin picked."""
    admin = _require_admin(request)
    email = email.strip().lower()
    if email and not valid_email(email):
        return error_redirect(request, "/admin/users", _("Enter a valid email address, or leave it empty."))
    if role not in ("user", "admin"):
        return error_redirect(request, "/admin/users", _("Unknown role."))
    token = await create_invite(admin["id"], email=email, role=role, allow_shared_llm=bool(allow_shared_llm))
    await _event(request, "invite_create", None, admin, f"for {email or 'any address'} as {role}")
    link = f"{BASE_URL}/signup?invite={token}"
    sent = False
    if email and mailer.mail_enabled():
        mailer.schedule(
            mailer.render_mail(
                "invite", normalize_language(language or current_language()), to=email, link=link, days=INVITE_TTL_DAYS
            )
        )
        await _event(request, "invite_email", None, admin, f"to {email}")
        sent = True
    return await _users_page(request, new_invite_link=link, new_invite_email=email, invite_sent=sent)


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite_route(request: Request, invite_id: str):
    admin = _require_admin(request)
    _validate_user_id(invite_id)
    if not await revoke_invite(invite_id):
        return error_redirect(request, "/admin/users", _("That invite was already used, revoked or expired."))
    await _event(request, "invite_revoke", None, admin)
    return success_redirect(request, "/admin/users", _("Invite revoked."))


@router.post("/users/{user_id}/reset-link", response_class=HTMLResponse)
async def create_reset_link(request: Request, user_id: str):
    """Shows the link once, on this response. Only its hash is stored."""
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", _("Change your own password under Settings > Security."))
    if email := await _protected(user_id):
        # Otherwise any admin could take over the operator's break-glass account.
        return error_redirect(
            request,
            "/admin/users",
            _("{email} is in VIRGIL_ADMIN_EMAILS; only its owner sets its password.").format(email=email),
        )
    user = await get_user_by_id(user_id)
    if not user:
        return error_redirect(request, "/admin/users", _("User account was not found."))
    if not user["is_active"]:
        return error_redirect(request, "/admin/users", _("Enable the account before resetting its password."))
    token = await create_password_reset(user_id, admin["id"])
    await _event(request, "reset_link_create", user_id, admin)
    return await _users_page(request, reset_link=f"{BASE_URL}/reset?token={token}", reset_email=user["email"])


@router.post("/users/{user_id}/reset-two-factor")
async def reset_two_factor(request: Request, user_id: str):
    """The way back for someone who lost their authenticator and recovery codes
    (#168): two-factor off, codes void, every session signed out."""
    from app.central_db import bump_session_epoch, delete_recovery_codes, update_user, void_sign_in_links

    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", _("Manage your own two-factor under Settings > Security."))
    if email := await _protected(user_id):
        # Otherwise any admin could take over the operator's break-glass account.
        return error_redirect(
            request,
            "/admin/users",
            _("{email} is in VIRGIL_ADMIN_EMAILS; only its owner manages its two-factor.").format(email=email),
        )
    user = await get_user_by_id(user_id)
    if not user:
        return error_redirect(request, "/admin/users", _("User account was not found."))
    await update_user(user_id, totp_enabled=0, totp_secret="")
    await delete_recovery_codes(user_id)
    await void_sign_in_links(user_id)
    await bump_session_epoch(user_id)
    await _event(request, "mfa_admin_reset", user_id, admin)
    return success_redirect(request, "/admin/users", _("Two-factor reset for {email}.").format(email=user["email"]))


@router.post("/users/{user_id}/shared-llm")
async def change_shared_llm(request: Request, user_id: str, allow: str = Form("")):
    """Whether the account may spend the server's LLM key when it has none of its own."""
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if not await get_user_by_id(user_id):
        return error_redirect(request, "/admin/users", _("User account was not found."))
    allowed = allow == "1"
    await update_user(user_id, allow_shared_llm=int(allowed))
    await _event(request, "llm_access_change", user_id, admin, "allowed" if allowed else "withdrawn")
    return success_redirect(
        request,
        "/admin/users",
        _("The account may use the server's LLM key.")
        if allowed
        else _("The account no longer uses the server's LLM key."),
    )


@router.post("/users/{user_id}/role")
async def change_role(request: Request, user_id: str, role: str = Form(...)):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if role not in ("user", "admin"):
        return error_redirect(request, "/admin/users", _("Unknown role."))
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", _("Your own role cannot be changed from the admin panel."))
    if role == "user" and (email := await _protected(user_id)):
        return error_redirect(
            request, "/admin/users", _("{email} is in VIRGIL_ADMIN_EMAILS and stays an admin.").format(email=email)
        )
    if not await get_user_by_id(user_id):
        return error_redirect(request, "/admin/users", _("User account was not found."))
    await update_user(user_id, role=role)
    if role == "user":
        await revoke_invites_created_by(user_id)
    await _event(request, "role_change", user_id, admin, f"to {role}")
    return success_redirect(request, "/admin/users", _("Role changed."))


@router.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", _("Your account cannot be disabled from the admin panel."))
    if email := await _protected(user_id):
        return error_redirect(
            request, "/admin/users", _("{email} is in VIRGIL_ADMIN_EMAILS and cannot be disabled.").format(email=email)
        )
    result = await disable_user_if_unsubscribed(user_id)
    if result == "not_found":
        return error_redirect(request, "/admin/users", _("User account was not found."))
    if result == "busy":
        return error_redirect(request, "/admin/users", _("Another account operation is already in progress."))
    if result == "subscribed":
        return error_redirect(
            request, "/admin/users", _("Disable the user's remote subscriptions before disabling the account.")
        )
    await revoke_invites_created_by(user_id)
    await _event(request, "user_disable", user_id, admin)
    return success_redirect(request, "/admin/users", _("User account disabled."))


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if not await get_user_by_id(user_id):
        return error_redirect(request, "/admin/users", _("User account was not found."))
    await update_user(user_id, is_active=1)
    await _event(request, "user_enable", user_id, admin)
    return success_redirect(request, "/admin/users", _("User account enabled."))


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return error_redirect(request, "/admin/users", _("Your account cannot be deleted from the admin panel."))
    if email := await _protected(user_id):
        return error_redirect(
            request, "/admin/users", _("{email} is in VIRGIL_ADMIN_EMAILS and cannot be deleted.").format(email=email)
        )
    result, db_filename = await delete_user_if_unsubscribed(user_id)
    if result == "busy":
        return error_redirect(request, "/admin/users", _("Another account operation is already in progress."))
    if result == "subscribed":
        return error_redirect(
            request, "/admin/users", _("Disable the user's remote subscriptions before deleting the account.")
        )
    if result == "not_found" or not db_filename:
        return error_redirect(request, "/admin/users", _("User account was not found."))
    delete_user_db(db_filename)
    delete_user_backups(db_filename)
    await _event(request, "user_delete", user_id, admin)
    return success_redirect(request, "/admin/users", _("User account deleted."))
