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
    create_session,
    hash_password,  # noqa: F401 — re-exported for use in tests
    session_cookie_header,
    validate_session,
    verify_password,
)
from app.central_db import create_user, get_user_by_email, get_user_by_id, update_user
from app.config import REGISTRATION_OPEN
from app.main import templates
from app.user_db import create_user_db

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Signup ---


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if not REGISTRATION_OPEN:
        return RedirectResponse("/login", status_code=303)

    session_token = request.cookies.get(SESSION_COOKIE, "")
    if session_token and validate_session(session_token):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse("auth_signup.html", {"request": request})


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    if not REGISTRATION_OPEN:
        return RedirectResponse("/login", status_code=303)

    email = email.strip().lower()
    display_name = display_name.strip()

    def _render_error(error: str):
        return templates.TemplateResponse(
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

    user = await create_user(email, password, display_name)
    await create_user_db(user["db_filename"])

    token = create_session(user["id"])
    response = RedirectResponse("/onboarding", status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Login ---


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if session_token and validate_session(session_token):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse("auth_login.html", {"request": request})


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    email = username.strip().lower()
    user = await get_user_by_email(email)

    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "auth_login.html",
            {"request": request, "error": "Invalid email or password."},
        )

    if not user["is_active"]:
        return templates.TemplateResponse(
            "auth_login.html",
            {"request": request, "error": "This account has been deactivated."},
        )

    # If MFA is enabled, redirect to MFA verification
    if user["totp_enabled"]:
        token = create_session(f"_mfa_pending:{user['id']}")
        response = RedirectResponse("/mfa/verify", status_code=303)
        response.headers["set-cookie"] = session_cookie_header(token)
        return response

    # No MFA — create full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    token = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Logout ---


@router.post("/logout")
async def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.headers["set-cookie"] = clear_session_cookie()
    return response


# --- MFA Setup ---


@router.get("/settings/mfa", response_class=HTMLResponse)
async def mfa_setup_page(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/login", status_code=303)

    if user["totp_enabled"]:
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {"request": request, "mfa_enabled": True, "qr_url": ""},
        )

    # Generate a new TOTP secret if not already stored
    secret = user["totp_secret"] or pyotp.random_base32()
    if not user["totp_secret"]:
        await update_user(user["id"], totp_secret=secret)

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user["email"], issuer_name="Virgil")

    return templates.TemplateResponse(
        "auth_mfa_setup.html",
        {"request": request, "mfa_enabled": False, "provisioning_uri": provisioning_uri, "secret": secret},
    )


@router.post("/settings/mfa/enable")
async def mfa_enable(request: Request, totp_code: str = Form(...)):
    user = getattr(request.state, "user", None)
    if not user or not user["totp_secret"]:
        return RedirectResponse("/settings/mfa", status_code=303)

    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(totp_code, valid_window=1):
        provisioning_uri = totp.provisioning_uri(name=user["email"], issuer_name="Virgil")
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {
                "request": request,
                "mfa_enabled": False,
                "provisioning_uri": provisioning_uri,
                "secret": user["totp_secret"],
                "error": "Invalid code. Please try again.",
            },
        )

    await update_user(user["id"], totp_enabled=1)
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/mfa/disable")
async def mfa_disable(request: Request, password: str = Form(...)):
    user = getattr(request.state, "user", None)
    if not user:
        return RedirectResponse("/settings", status_code=303)

    if not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {"request": request, "mfa_enabled": True, "error": "Invalid password."},
        )

    await update_user(user["id"], totp_enabled=0, totp_secret="")
    return RedirectResponse("/settings", status_code=303)


# --- MFA Verify (during login) ---


@router.get("/mfa/verify", response_class=HTMLResponse)
async def mfa_verify_page(request: Request):
    session_token = request.cookies.get(SESSION_COOKIE, "")
    pending = validate_session(session_token) if session_token else None
    if not pending or not pending.startswith("_mfa_pending:"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("auth_mfa_verify.html", {"request": request})


@router.post("/mfa/verify")
async def mfa_verify_submit(request: Request, totp_code: str = Form(...)):
    session_token = request.cookies.get(SESSION_COOKIE, "")
    pending = validate_session(session_token) if session_token else None
    if not pending or not pending.startswith("_mfa_pending:"):
        return RedirectResponse("/login", status_code=303)

    user_id = pending.removeprefix("_mfa_pending:")
    user = await get_user_by_id(user_id)
    if not user:
        return RedirectResponse("/login", status_code=303)

    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(totp_code, valid_window=1):
        return templates.TemplateResponse(
            "auth_mfa_verify.html",
            {"request": request, "error": "Invalid code. Please try again."},
        )

    # MFA verified — upgrade to full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    token = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- QR Code image endpoint ---


@router.get("/mfa/qr.png")
async def mfa_qr_image(request: Request, uri: str = ""):
    if not uri or not uri.startswith("otpauth://"):
        return Response(status_code=400)
    img = qrcode.make(uri, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png")
