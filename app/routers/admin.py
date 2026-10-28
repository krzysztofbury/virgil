"""Admin panel — user management."""

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

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


USERS_TAB = "/settings?tab=users"
SERVER_TAB = "/settings?tab=server"


async def users_context() -> dict:
    """What the Users tab of Settings shows."""
    users = await get_all_users()
    return {
        "users": users,
        "total": len(users),
        "registration_open": current.REGISTRATION_OPEN,
        "admin_emails": ADMIN_EMAILS,
        "events": await list_auth_events(),
        "invites": await list_open_invites(),
        "invite_days": INVITE_TTL_DAYS,
        "reset_minutes": RESET_TTL_MINUTES,
    }


async def _users_page(request: Request, **extra):
    from app.routers.settings import render_settings

    return await render_settings(request, "users", **extra)


@router.get("/users")
async def list_users(request: Request):
    """The admin panel moved into Settings (#179); old links still land there."""
    _require_admin(request)
    return RedirectResponse(USERS_TAB, status_code=303)


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
        return error_redirect(request, USERS_TAB, _("Enter a valid email address, or leave it empty."))
    if role not in ("user", "admin"):
        return error_redirect(request, USERS_TAB, _("Unknown role."))
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
        return error_redirect(request, USERS_TAB, _("That invite was already used, revoked or expired."))
    await _event(request, "invite_revoke", None, admin)
    return success_redirect(request, USERS_TAB, _("Invite revoked."))


@router.post("/users/{user_id}/reset-link", response_class=HTMLResponse)
async def create_reset_link(request: Request, user_id: str):
    """Shows the link once, on this response. Only its hash is stored."""
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return error_redirect(request, USERS_TAB, _("Change your own password under Settings > Security."))
    if email := await _protected(user_id):
        # Otherwise any admin could take over the operator's break-glass account.
        return error_redirect(
            request,
            USERS_TAB,
            _("{email} is in VIRGIL_ADMIN_EMAILS; only its owner sets its password.").format(email=email),
        )
    user = await get_user_by_id(user_id)
    if not user:
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    if not user["is_active"]:
        return error_redirect(request, USERS_TAB, _("Enable the account before resetting its password."))
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
        return error_redirect(request, USERS_TAB, _("Manage your own two-factor under Settings > Security."))
    if email := await _protected(user_id):
        # Otherwise any admin could take over the operator's break-glass account.
        return error_redirect(
            request,
            USERS_TAB,
            _("{email} is in VIRGIL_ADMIN_EMAILS; only its owner manages its two-factor.").format(email=email),
        )
    user = await get_user_by_id(user_id)
    if not user:
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    await update_user(user_id, totp_enabled=0, totp_secret="")
    await delete_recovery_codes(user_id)
    await void_sign_in_links(user_id)
    await bump_session_epoch(user_id)
    await _event(request, "mfa_admin_reset", user_id, admin)
    return success_redirect(request, USERS_TAB, _("Two-factor reset for {email}.").format(email=user["email"]))


@router.post("/users/{user_id}/shared-llm")
async def change_shared_llm(request: Request, user_id: str, allow: str = Form("")):
    """Whether the account may spend the server's LLM key when it has none of its own."""
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if not await get_user_by_id(user_id):
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    allowed = allow == "1"
    await update_user(user_id, allow_shared_llm=int(allowed))
    await _event(request, "llm_access_change", user_id, admin, "allowed" if allowed else "withdrawn")
    return success_redirect(
        request,
        USERS_TAB,
        _("The account may use the server's LLM key.")
        if allowed
        else _("The account no longer uses the server's LLM key."),
    )


@router.post("/users/{user_id}/role")
async def change_role(request: Request, user_id: str, role: str = Form(...)):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if role not in ("user", "admin"):
        return error_redirect(request, USERS_TAB, _("Unknown role."))
    if user_id == admin["id"]:
        return error_redirect(request, USERS_TAB, _("Your own role cannot be changed from the admin panel."))
    if role == "user" and (email := await _protected(user_id)):
        return error_redirect(
            request, USERS_TAB, _("{email} is in VIRGIL_ADMIN_EMAILS and stays an admin.").format(email=email)
        )
    if not await get_user_by_id(user_id):
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    await update_user(user_id, role=role)
    if role == "user":
        await revoke_invites_created_by(user_id)
    await _event(request, "role_change", user_id, admin, f"to {role}")
    return success_redirect(request, USERS_TAB, _("Role changed."))


@router.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if user_id == admin["id"]:
        return error_redirect(request, USERS_TAB, _("Your account cannot be disabled from the admin panel."))
    if email := await _protected(user_id):
        return error_redirect(
            request, USERS_TAB, _("{email} is in VIRGIL_ADMIN_EMAILS and cannot be disabled.").format(email=email)
        )
    result = await disable_user_if_unsubscribed(user_id)
    if result == "not_found":
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    if result == "busy":
        return error_redirect(request, USERS_TAB, _("Another account operation is already in progress."))
    if result == "subscribed":
        return error_redirect(
            request, USERS_TAB, _("Disable the user's remote subscriptions before disabling the account.")
        )
    await revoke_invites_created_by(user_id)
    await _event(request, "user_disable", user_id, admin)
    return success_redirect(request, USERS_TAB, _("User account disabled."))


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    if not await get_user_by_id(user_id):
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    await update_user(user_id, is_active=1)
    await _event(request, "user_enable", user_id, admin)
    return success_redirect(request, USERS_TAB, _("User account enabled."))


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    _validate_user_id(user_id)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return error_redirect(request, USERS_TAB, _("Your account cannot be deleted from the admin panel."))
    if email := await _protected(user_id):
        return error_redirect(
            request, USERS_TAB, _("{email} is in VIRGIL_ADMIN_EMAILS and cannot be deleted.").format(email=email)
        )
    result, db_filename = await delete_user_if_unsubscribed(user_id)
    if result == "busy":
        return error_redirect(request, USERS_TAB, _("Another account operation is already in progress."))
    if result == "subscribed":
        return error_redirect(
            request, USERS_TAB, _("Disable the user's remote subscriptions before deleting the account.")
        )
    if result == "not_found" or not db_filename:
        return error_redirect(request, USERS_TAB, _("User account was not found."))
    delete_user_db(db_filename)
    delete_user_backups(db_filename)
    await _event(request, "user_delete", user_id, admin)
    return success_redirect(request, USERS_TAB, _("User account deleted."))


# --- Server settings (#179) ---

_pending_alerts: set = set()


async def _server_change(request: Request, admin: dict, what: str, alert_url: str) -> None:
    """Record a server change, and tell the alert webhook that was set before it.

    The earlier webhook hears about it, so a session that changes the webhook or
    the off-site destination cannot do so without the operator seeing it. The
    record names the setting, never the value."""
    await _event(request, "server_setting", None, admin, what)
    if alert_url:
        import asyncio

        from app.alerts import send_alert

        task = asyncio.create_task(send_alert(alert_url, f"Virgil: {admin['email']} changed {what}."))
        _pending_alerts.add(task)
        task.add_done_callback(_pending_alerts.discard)


async def server_context() -> dict:
    """What the Server tab of Settings shows. Secrets only as their last characters."""
    from app import config, server_settings
    from app.i18n import _ as translate
    from app.services.server_llm import list_providers

    groups = []
    for group, title in server_settings.GROUPS.items():
        rows = []
        for item in server_settings.SETTINGS:
            if item.group != group:
                continue
            value = getattr(current, item.attribute)
            rows.append(
                {
                    "attribute": item.attribute,
                    "env_name": item.env_name,
                    "label": translate(item.label),
                    "kind": item.kind,
                    "secret": item.secret,
                    "choices": item.choices,
                    "source": server_settings.source(item.attribute),
                    "value": "" if item.secret else ("" if value is None else str(value)),
                    "checked": value is True,
                    "masked": server_settings.masked(item.attribute) if item.secret else "",
                }
            )
        groups.append({"id": group, "title": translate(title), "rows": rows})
    env_key = config.INTERNAL_LLM_KEY
    return {
        "server_groups": groups,
        "server_llm_providers": await list_providers(),
        "env_llm_model": config.INTERNAL_LLM_MODEL if env_key else "",
        "env_llm_key": f"...{env_key[-4:]}" if len(env_key) > 8 else ("****" if env_key else ""),
    }


@router.post("/server/settings/{attribute}")
async def save_server_setting(request: Request, attribute: str, value: str = Form("")):
    from app import server_settings

    admin = _require_admin(request)
    alert_url = current.ALERT_WEBHOOK_URL
    try:
        item = server_settings.setting(attribute)
    except server_settings.SettingError:
        return error_redirect(request, SERVER_TAB, _("That setting cannot be changed in the app."))
    back = f"{SERVER_TAB}#{item.attribute}"
    if item.secret and not value.strip():
        return error_redirect(request, back, _("Enter the new value. Reset returns to the environment's."))
    try:
        await server_settings.save(item.attribute, value, by=admin["email"])
    except server_settings.SettingError as exc:
        return error_redirect(request, back, _(str(exc)))
    await _server_change(request, admin, item.env_name, alert_url)
    return success_redirect(request, back, _("Saved. It applies now."))


@router.post("/server/settings/{attribute}/reset")
async def reset_server_setting(request: Request, attribute: str):
    from app import server_settings

    admin = _require_admin(request)
    alert_url = current.ALERT_WEBHOOK_URL
    try:
        item = server_settings.setting(attribute)
    except server_settings.SettingError:
        return error_redirect(request, SERVER_TAB, _("That setting cannot be changed in the app."))
    await server_settings.reset(item.attribute)
    await _server_change(request, admin, f"{item.env_name} (back to the environment's value)", alert_url)
    return success_redirect(request, f"{SERVER_TAB}#{item.attribute}", _("The environment's value applies again."))


@router.post("/server/llm/add")
async def add_server_llm(request: Request, provider: str = Form(...), model: str = Form(...), api_key: str = Form(...)):
    from app.services.server_llm import add_provider
    from app.validation import truncate

    admin = _require_admin(request)
    provider, model = truncate(provider.strip(), 50), truncate(model.strip(), 200)
    if not provider or not model or not api_key.strip():
        return error_redirect(request, f"{SERVER_TAB}#server-ai", _("Provider, model, and API key are required."))
    alert_url = current.ALERT_WEBHOOK_URL
    await add_provider(provider, model, api_key.strip())
    await _server_change(request, admin, f"the server AI keys (added {model})", alert_url)
    return success_redirect(request, f"{SERVER_TAB}#server-ai", _("AI key added."))


@router.post("/server/llm/activate")
async def activate_server_llm(request: Request, provider_id: int = Form(...)):
    from app.services.server_llm import activate_provider

    admin = _require_admin(request)
    alert_url = current.ALERT_WEBHOOK_URL
    if not await activate_provider(provider_id):
        return error_redirect(request, f"{SERVER_TAB}#server-ai", _("That AI key no longer exists."))
    await _server_change(request, admin, "the server AI key in use", alert_url)
    return success_redirect(request, f"{SERVER_TAB}#server-ai", _("AI key in use."))


@router.post("/server/llm/delete")
async def delete_server_llm(request: Request, provider_id: int = Form(...)):
    from app.services.server_llm import delete_provider

    admin = _require_admin(request)
    alert_url = current.ALERT_WEBHOOK_URL
    if not await delete_provider(provider_id):
        return error_redirect(request, f"{SERVER_TAB}#server-ai", _("That AI key no longer exists."))
    await _server_change(request, admin, "the server AI keys (removed one)", alert_url)
    return success_redirect(request, f"{SERVER_TAB}#server-ai", _("AI key removed."))
