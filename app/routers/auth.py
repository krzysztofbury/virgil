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
    mfa_pending_user_id,
    session_cookie_header,
    signed_in_user_id,
    verify_password,
)
from app.central_db import (
    bump_session_epoch,
    claim_mfa_attempt,
    count_users,
    create_user,
    finish_mfa_attempt,
    get_user_by_email,
    get_user_by_id,
    update_user,
)
from app.config import REGISTRATION_OPEN
from app.feedback import error_redirect, feedback_url, success_redirect
from app.main import templates
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


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if not await registration_allowed():
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"request": request, "info": "Registration is closed on this server.", "registration_open": False},
        )

    if signed_in_user_id(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(request, "auth_signup.html", {"request": request})


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if not await registration_allowed():
        return error_redirect(request, "/login", "Registration is closed on this server.")

    email = email.strip().lower()
    display_name = display_name.strip()

    def _render_error(error: str):
        return templates.TemplateResponse(
            request,
            "auth_signup.html",
            {"request": request, "error": error, "email": email, "display_name": display_name},
        )

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return _render_error("Please enter a valid email address.")

    if len(password) < 8:
        return _render_error("Password must be at least 8 characters.")

    if password != password_confirm:
        return _render_error("Passwords do not match.")

    existing = await get_user_by_email(email)
    if existing:
        return _render_error("An account with that email already exists.")

    # With registration closed we're only here via the bootstrap-first-user
    # rule — make the INSERT itself enforce it so two concurrent first signups
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
        await delete_user(user["id"])
        return _render_error("Could not create your account. Please try again.")

    token = create_session(user["id"], user["session_epoch"])
    target = feedback_url("/onboarding", msg="Account created.")
    response = RedirectResponse(target, status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Login ---


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if signed_in_user_id(request.cookies.get(SESSION_COOKIE, "")):
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
    password_ok = verify_password(password, password_hash)

    if not user or not password_ok:
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
    return _resign_after_mfa_change(request, user["id"], await bump_session_epoch(user["id"]), "MFA enabled.")


@router.post("/settings/mfa/disable")
async def mfa_disable(request: Request, password: str = Form(...)):
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/settings", "MFA could not be disabled.")

    # verify_password raises on empty input — treat it as a wrong password.
    if not password or not verify_password(password, user["password_hash"]):
        return _mfa_setup_response(request, mfa_enabled=True, error="Invalid password.")

    await update_user(user["id"], totp_enabled=0, totp_secret="")
    return _resign_after_mfa_change(request, user["id"], await bump_session_epoch(user["id"]), "MFA disabled.")


def _resign_after_mfa_change(request: Request, user_id: str, epoch: int, message: str):
    """Every other session is signed out by the bumped epoch; this browser keeps going."""
    response = success_redirect(request, "/settings", message)
    response.headers.append("set-cookie", session_cookie_header(create_session(user_id, epoch)))
    return response


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
        return templates.TemplateResponse(
            request,
            "auth_mfa_verify.html",
            {"request": request, "error": "Invalid code. Please try again."},
        )

    # MFA verified — upgrade to full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
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
