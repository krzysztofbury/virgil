import io
import logging
import re

import pyotp
import qrcode
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import app.auth as auth_module
from app.auth import (
    SESSION_COOKIE,
    clear_session_cookie,
    create_session,
    hash_password,
    session_cookie_header,
    validate_session,
    verify_password,
)
from app.db import get_db
from app.main import templates

logger = logging.getLogger(__name__)
router = APIRouter()


async def _get_user(db) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM auth_users WHERE id = 1")
    return dict(rows[0]) if rows else None


# --- Initial Setup ---


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = await get_db()
    user = await _get_user(db)
    if user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("auth_setup.html", {"request": request})


@router.post("/setup")
async def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    db = await get_db()
    user = await _get_user(db)
    if user:
        return RedirectResponse("/login", status_code=303)

    email = username.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return templates.TemplateResponse(
            "auth_setup.html",
            {"request": request, "error": "Username must be a valid email address."},
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            "auth_setup.html",
            {"request": request, "error": "Password must be at least 8 characters."},
        )

    if password != password_confirm:
        return templates.TemplateResponse(
            "auth_setup.html",
            {"request": request, "error": "Passwords do not match."},
        )

    pw_hash = hash_password(password)
    await db.execute(
        "INSERT INTO auth_users (id, username, password_hash) VALUES (1, ?, ?)",
        (email, pw_hash),
    )
    await db.commit()

    # Invalidate the "user exists" cache so auth middleware picks up the new user
    auth_module._user_exists = True

    # Auto-login after setup
    token = create_session(email)
    response = RedirectResponse("/onboarding", status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    return response


# --- Login ---


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    db = await get_db()
    user = await _get_user(db)
    if not user:
        return RedirectResponse("/setup", status_code=303)

    # If already authenticated, redirect to home
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
    db = await get_db()
    user = await _get_user(db)
    if not user:
        return RedirectResponse("/setup", status_code=303)

    if username.strip().lower() != user["username"] or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "auth_login.html",
            {"request": request, "error": "Invalid username or password."},
        )

    # If MFA is enabled, redirect to MFA verification
    if user["totp_enabled"]:
        # Store a temporary pre-MFA session
        token = create_session(f"_mfa_pending:{username.strip()}")
        response = RedirectResponse("/mfa/verify", status_code=303)
        response.headers["set-cookie"] = session_cookie_header(token)
        return response

    # No MFA — create full session
    token = create_session(username.strip())
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
    db = await get_db()
    user = await _get_user(db)
    if not user:
        return RedirectResponse("/setup", status_code=303)

    if user["totp_enabled"]:
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {"request": request, "mfa_enabled": True, "qr_url": ""},
        )

    # Generate a new TOTP secret
    secret = user["totp_secret"] or pyotp.random_base32()
    if not user["totp_secret"]:
        await db.execute("UPDATE auth_users SET totp_secret = ? WHERE id = 1", (secret,))
        await db.commit()

    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user["username"], issuer_name="Virgil")

    return templates.TemplateResponse(
        "auth_mfa_setup.html",
        {"request": request, "mfa_enabled": False, "provisioning_uri": provisioning_uri, "secret": secret},
    )


@router.post("/settings/mfa/enable")
async def mfa_enable(request: Request, totp_code: str = Form(...)):
    db = await get_db()
    user = await _get_user(db)
    if not user or not user["totp_secret"]:
        return RedirectResponse("/settings/mfa", status_code=303)

    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(totp_code, valid_window=1):
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {
                "request": request,
                "mfa_enabled": False,
                "provisioning_uri": totp.provisioning_uri(name=user["username"], issuer_name="Virgil"),
                "secret": user["totp_secret"],
                "error": "Invalid code. Please try again.",
            },
        )

    await db.execute("UPDATE auth_users SET totp_enabled = 1 WHERE id = 1")
    await db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/settings/mfa/disable")
async def mfa_disable(request: Request, password: str = Form(...)):
    db = await get_db()
    user = await _get_user(db)
    if not user:
        return RedirectResponse("/settings", status_code=303)

    if not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "auth_mfa_setup.html",
            {"request": request, "mfa_enabled": True, "error": "Invalid password."},
        )

    await db.execute("UPDATE auth_users SET totp_enabled = 0, totp_secret = '' WHERE id = 1")
    await db.commit()
    return RedirectResponse("/settings", status_code=303)


# --- MFA Verify (during login) ---


@router.get("/mfa/verify", response_class=HTMLResponse)
async def mfa_verify_page(request: Request):
    # Check for pending MFA session
    session_token = request.cookies.get(SESSION_COOKIE, "")
    username = validate_session(session_token) if session_token else None
    if not username or not username.startswith("_mfa_pending:"):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("auth_mfa_verify.html", {"request": request})


@router.post("/mfa/verify")
async def mfa_verify_submit(request: Request, totp_code: str = Form(...)):
    session_token = request.cookies.get(SESSION_COOKIE, "")
    username = validate_session(session_token) if session_token else None
    if not username or not username.startswith("_mfa_pending:"):
        return RedirectResponse("/login", status_code=303)

    real_username = username.removeprefix("_mfa_pending:")
    db = await get_db()
    user = await _get_user(db)
    if not user or user["username"] != real_username:
        return RedirectResponse("/login", status_code=303)

    totp = pyotp.TOTP(user["totp_secret"])
    if not totp.verify(totp_code, valid_window=1):
        return templates.TemplateResponse(
            "auth_mfa_verify.html",
            {"request": request, "error": "Invalid code. Please try again."},
        )

    # MFA verified — upgrade to full session
    token = create_session(real_username)
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
