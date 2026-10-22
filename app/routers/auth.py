import io
import logging
import re
from datetime import UTC, datetime

import pyotp
import qrcode
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_mfa_pending_session,
    create_session,
    hash_password,  # noqa: F401 — re-exported for use in tests
    hash_password_async,
    mfa_pending_user_id,
    session_cookie_header,
    signed_in_user,
    verify_password_async,
)
from app.central_db import (
    bump_session_epoch,
    claim_mfa_attempt,
    count_users,
    create_user,
    create_user_from_invite,
    finish_mfa_attempt,
    get_open_invite,
    get_user_by_email,
    get_user_by_id,
    record_auth_event,
    reopen_invite_used_by,
    update_user,
)
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.feedback import error_redirect, feedback_url, success_redirect
from app.main import templates
from app.rate_limit import client_ip
from app.user_db import create_user_db

logger = logging.getLogger(__name__)
router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}

# Constant-time-ish login for unknown emails: verify against this throwaway
# hash so response timing doesn't reveal whether an account exists.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-timing")

# Wrong TOTP codes per account before verification locks. The per-IP rate limit
# alone lets a rotating attacker keep guessing a six-digit code indefinitely.
MFA_MAX_ATTEMPTS = 5
MFA_LOCK_SECONDS = 15 * 60

SECURITY = "/settings?tab=security"


async def _event(request: Request, event: str, user_id: str | None = None, detail: str = "") -> None:
    await record_auth_event(event, user_id=user_id, ip=client_ip(request), detail=detail)


def _totp_secret_plain(stored: str | None) -> str:
    """TOTP secrets are Fernet-encrypted in the central DB; rows written before
    encryption existed are plaintext base32 — fall back so old MFA keeps working
    (they re-encrypt on the next enable)."""
    if not stored:
        return ""
    from app.services.encryption import decrypt

    try:
        return decrypt(stored)
    except Exception:
        return stored


def _totp_secret_stored(plain: str) -> str:
    from app.services.encryption import encrypt

    return encrypt(plain)


def _qr_png(data: str) -> bytes:
    img = qrcode.make(data, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _mfa_setup_response(request: Request, **context):
    """Setup HTML. Pages that include the TOTP secret must not be cached."""
    response = templates.TemplateResponse(request, "auth_mfa_setup.html", {"request": request, **context})
    if context.get("secret"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def registration_allowed() -> bool:
    """Signups are allowed when explicitly opened, or on a fresh install —
    the very first account must always be creatable (bootstrap owner)."""
    if REGISTRATION_OPEN:
        return True
    return await count_users() == 0


# --- Signup ---


INVITE_UNUSABLE = "This invite link is invalid, used or expired. Ask for a new one."
RESERVED_ADDRESS = "This address is reserved on this server. Ask an admin for an invite made for it."


def _signup_response(request: Request, **context):
    """The signup page. With an invite, its secret is in the form, so nothing may cache the page."""
    response = templates.TemplateResponse(request, "auth_signup.html", {"request": request, **context})
    if context.get("invite"):
        response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, invite: str = ""):
    if invite:
        if await signed_in_user(request.cookies.get(SESSION_COOKIE, "")):
            return RedirectResponse("/", status_code=303)
        open_invite = await get_open_invite(invite)
        if not open_invite:
            return templates.TemplateResponse(
                request,
                "auth_login.html",
                {"request": request, "info": INVITE_UNUSABLE, "registration_open": await registration_allowed()},
                headers=_NO_STORE,
            )
        return _signup_response(
            request, invite=invite, email=open_invite["email"], email_locked=bool(open_invite["email"])
        )

    if not await registration_allowed():
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"request": request, "info": "Registration is closed on this server.", "registration_open": False},
        )

    if await signed_in_user(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(request, "auth_signup.html", {"request": request})


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
    invite: str = Form(""),
):
    if not invite and not await registration_allowed():
        return error_redirect(request, "/login", "Registration is closed on this server.")

    email = email.strip().lower()
    display_name = display_name.strip()
    invite_email = ""
    if invite:
        open_invite = await get_open_invite(invite)
        if not open_invite:
            return error_redirect(request, "/login", INVITE_UNUSABLE)
        invite_email = open_invite["email"]

    def _render_error(error: str):
        return _signup_response(
            request,
            error=error,
            email=invite_email or email,
            display_name=display_name,
            invite=invite,
            email_locked=bool(invite_email),
        )

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _render_error("Please enter a valid email address.")

    if len(password) < 8:
        return _render_error("Password must be at least 8 characters.")

    if password != password_confirm:
        return _render_error("Passwords do not match.")

    if invite:
        # The invite is checked and used up in the same transaction as the insert.
        status, user = await create_user_from_invite(invite, email, password, display_name)
        if status == "invalid":
            return error_redirect(request, "/login", INVITE_UNUSABLE)
        if status == "email_mismatch":
            return _render_error("This invite is for a different email address.")
        if status == "email_reserved":
            return _render_error(RESERVED_ADDRESS)
        if status == "email_taken":
            return _render_error("An account with that email already exists.")
    else:
        existing = await get_user_by_email(email)
        if existing:
            return _render_error("An account with that email already exists.")
        # Open registration verifies no address, and VIRGIL_ADMIN_EMAILS makes one an
        # admin: only the first account, the operator's, may claim such an address here.
        if email in ADMIN_EMAILS and await count_users() > 0:
            return _render_error(RESERVED_ADDRESS)

        # With registration closed we're only here via the bootstrap-first-user
        # rule, so the INSERT itself enforces it and two concurrent first signups
        # can't both slip through the count check.
        user = await create_user(email, password, display_name, only_if_first=not REGISTRATION_OPEN)
        if user is None:
            return error_redirect(request, "/login", "Registration is closed on this server.")

    try:
        await create_user_db(user["db_filename"])
    except Exception:
        # Never leave an account whose database was never provisioned — it
        # could log in but every request would fail, and the email would be
        # permanently burned.
        from app.central_db import delete_user

        logger.exception("User DB provisioning failed for %s — rolling back account", email)
        if invite:
            await reopen_invite_used_by(user["id"])
        await delete_user(user["id"])
        return _render_error("Could not create your account. Please try again.")

    await _event(request, "invite_consume" if invite else "signup", user["id"])
    token = create_session(user["id"], user["session_epoch"])
    target = feedback_url("/onboarding", msg="Account created.")
    response = RedirectResponse(target, status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Login ---


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await signed_in_user(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(
        request, "auth_login.html", {"request": request, "registration_open": await registration_allowed()}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(""),
):
    email = username.strip().lower()

    # verify_password raises on empty input — reject it up front with the same
    # generic error instead of a 500.
    if not password:
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "request": request,
                "error": "Invalid email or password.",
                "registration_open": await registration_allowed(),
            },
        )

    user = await get_user_by_email(email)

    # Burn a bcrypt verify even for unknown emails so timing stays uniform.
    password_hash = user["password_hash"] if user else _DUMMY_PASSWORD_HASH
    password_ok = await verify_password_async(password, password_hash)

    if not user or not password_ok:
        await _event(
            request, "login_fail", user["id"] if user else None, "wrong password" if user else "unknown account"
        )
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "request": request,
                "error": "Invalid email or password.",
                "registration_open": await registration_allowed(),
            },
        )

    if not user["is_active"]:
        await _event(request, "login_fail", user["id"], "account disabled")
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "request": request,
                "error": "This account has been deactivated.",
                "registration_open": await registration_allowed(),
            },
        )

    # If MFA is enabled, redirect to MFA verification
    if user["totp_enabled"]:
        token = create_mfa_pending_session(user["id"])
        response = RedirectResponse("/mfa/verify", status_code=303)
        response.headers["set-cookie"] = session_cookie_header(token)
        return response

    # No MFA — create full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    await _event(request, "login_ok", user["id"])
    token = create_session(user["id"], user["session_epoch"])
    response = RedirectResponse(feedback_url("/", msg="Signed in."), status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Logout ---


@router.post("/logout")
async def logout(request: Request):
    # Clearing the cookie is not enough: a copied token would stay valid for its
    # full lifetime. Bumping the epoch signs out every session of the account.
    user = getattr(request.state, "user", None)
    if user:
        await bump_session_epoch(user["id"])
        await _event(request, "logout", user["id"])
    response = RedirectResponse(feedback_url("/login", msg="Signed out."), status_code=303)
    response.headers["set-cookie"] = clear_session_cookie()
    return response


# --- MFA Setup ---


@router.get("/settings/mfa", response_class=HTMLResponse)
async def mfa_setup_page(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if user["totp_enabled"]:
        return _mfa_setup_response(request, mfa_enabled=True)

    # Generate a new TOTP secret if not already stored (encrypted at rest).
    # The QR image is a separate same-origin GET; the secret never goes in a URL.
    secret = _totp_secret_plain(user["totp_secret"]) or pyotp.random_base32()
    if not user["totp_secret"]:
        await update_user(user["id"], totp_secret=_totp_secret_stored(secret))

    return _mfa_setup_response(request, mfa_enabled=False, secret=secret)


@router.post("/settings/mfa/enable")
async def mfa_enable(request: Request, totp_code: str = Form(...)):
    user = getattr(request.state, "user", None)
    if not user or not user["totp_secret"]:
        return error_redirect(request, "/settings/mfa", "MFA setup is unavailable. Start setup again.")

    secret = _totp_secret_plain(user["totp_secret"])
    totp = pyotp.TOTP(secret)
    if not totp.verify(totp_code, valid_window=1):
        return _mfa_setup_response(
            request,
            mfa_enabled=False,
            secret=secret,
            error="Invalid code. Please try again.",
        )

    # Re-writing the secret lazily migrates legacy plaintext rows to encrypted.
    await update_user(user["id"], totp_enabled=1, totp_secret=_totp_secret_stored(secret))
    await _event(request, "mfa_enable", user["id"])
    return _resign(request, user["id"], await bump_session_epoch(user["id"]), "MFA enabled.")


@router.post("/settings/mfa/disable")
async def mfa_disable(request: Request, password: str = Form(...)):
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/settings", "MFA could not be disabled.")

    # verify_password raises on empty input — treat it as a wrong password.
    if not password or not await verify_password_async(password, user["password_hash"]):
        return _mfa_setup_response(request, mfa_enabled=True, error="Invalid password.")

    await update_user(user["id"], totp_enabled=0, totp_secret="")
    await _event(request, "mfa_disable", user["id"])
    return _resign(request, user["id"], await bump_session_epoch(user["id"]), "MFA disabled.")


def _resign(request: Request, user_id: str, epoch: int, message: str, target: str = "/settings"):
    """Every other session is signed out by the bumped epoch; this browser keeps going."""
    response = success_redirect(request, target, message)
    response.headers.append("set-cookie", session_cookie_header(create_session(user_id, epoch)))
    return response


# --- Password change ---


@router.post("/settings/password")
async def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/login", "Sign in again to change your password.")
    # verify_password raises on empty input, so an empty one is a wrong password.
    if not current_password or not await verify_password_async(current_password, user["password_hash"]):
        return error_redirect(request, SECURITY, "Current password is incorrect.")
    if len(new_password) < 8:
        return error_redirect(request, SECURITY, "New password must be at least 8 characters.")
    if new_password != new_password_confirm:
        return error_redirect(request, SECURITY, "New passwords do not match.")

    await update_user(user["id"], password_hash=await hash_password_async(new_password))
    epoch = await bump_session_epoch(user["id"])
    await _event(request, "password_change", user["id"])
    return _resign(request, user["id"], epoch, "Password changed. Other devices are signed out.", SECURITY)


# --- MFA Verify (during login) ---


@router.get("/mfa/verify", response_class=HTMLResponse)
async def mfa_verify_page(request: Request):
    if not mfa_pending_user_id(request.cookies.get(SESSION_COOKIE, "")):
        return error_redirect(request, "/login", "MFA session expired. Sign in again.")
    return templates.TemplateResponse(request, "auth_mfa_verify.html", {"request": request})


@router.post("/mfa/verify")
async def mfa_verify_submit(request: Request, totp_code: str = Form(...)):
    user_id = mfa_pending_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        return error_redirect(request, "/login", "MFA session expired. Sign in again.")

    user = await get_user_by_id(user_id)
    if not user or not user["is_active"]:
        return error_redirect(request, "/login", "MFA session expired. Sign in again.")

    now = datetime.now(UTC)
    if not await claim_mfa_attempt(user["id"], now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS):
        await _event(request, "mfa_lock", user["id"])
        return templates.TemplateResponse(
            request,
            "auth_mfa_verify.html",
            {
                "request": request,
                "error": f"Too many invalid codes. Try again in {MFA_LOCK_SECONDS // 60} minutes.",
            },
            status_code=429,
        )

    totp = pyotp.TOTP(_totp_secret_plain(user["totp_secret"]))
    code_ok = totp.verify(totp_code, valid_window=1)
    await finish_mfa_attempt(user["id"], code_ok, now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS)
    if not code_ok:
        await _event(request, "mfa_fail", user["id"])
        return templates.TemplateResponse(
            request,
            "auth_mfa_verify.html",
            {"request": request, "error": "Invalid code. Please try again."},
        )

    # MFA verified — upgrade to full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    await _event(request, "login_ok", user["id"], "with MFA")
    token = create_session(user["id"], user["session_epoch"])
    response = RedirectResponse(feedback_url("/", msg="Signed in."), status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- QR Code image endpoint ---


@router.get("/mfa/qr.png")
async def mfa_qr_image(request: Request):
    """QR for the signed-in user's unfinished MFA setup.

    Built from the stored secret. The route takes no query parameters, so the
    setup page cannot put the secret in a URL.
    """
    user = getattr(request.state, "user", None)
    secret = _totp_secret_plain(user["totp_secret"]) if user else ""
    if not user or user["totp_enabled"] or not secret:
        return Response(status_code=404, headers=_NO_STORE)
    uri = pyotp.TOTP(secret).provisioning_uri(name=user["email"], issuer_name="Virgil")
    return Response(content=_qr_png(uri), media_type="image/png", headers=_NO_STORE)
