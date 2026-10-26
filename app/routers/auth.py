import io
import logging
from datetime import UTC, datetime

import pyotp
import qrcode
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import config
from app.auth import (
    LANGUAGE_CHOSEN_COOKIE,
    SESSION_COOKIE,
    clear_session_cookie,
    create_mfa_pending_session,
    create_session,
    hash_password,  # noqa: F401 — re-exported for use in tests
    hash_password_async,
    language_chosen_cookie_header,
    language_cookie_header,
    mfa_pending_user_id,
    session_cookie_header,
    session_is_fresh,
    signed_in_user,
    verify_password_async,
)
from app.central_db import (
    SIGN_IN_LINK_TTL_MINUTES,
    bump_session_epoch,
    claim_mfa_attempt,
    complete_password_reset,
    count_users,
    create_sign_in_link,
    create_user,
    create_user_from_invite,
    delete_recovery_codes,
    finish_mfa_attempt,
    get_open_invite,
    get_open_reset,
    get_open_sign_in_link,
    get_user_by_email,
    get_user_by_id,
    record_auth_event,
    reopen_invite_used_by,
    replace_recovery_codes,
    update_user,
    use_recovery_code,
    use_sign_in_link,
)
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.feedback import error_redirect, feedback_url, success_redirect
from app.i18n import LANGUAGE_COOKIE, N_, _, current_language, ngettext, normalize_language, set_language
from app.main import templates
from app.rate_limit import client_ip
from app.services import mailer
from app.user_db import create_user_db
from app.validation import valid_email

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


INVITE_UNUSABLE = N_("This invite link is invalid, used or expired. Ask for a new one.")
RESERVED_ADDRESS = N_("This address is reserved on this server. Ask an admin for an invite made for it.")
RESET_UNUSABLE = N_("This reset link is invalid, used or expired. Ask an admin for a new one.")
LINK_UNUSABLE = N_("That link has expired. Ask for a new one.")


def _signup_response(request: Request, **context):
    """The signup page. With an invite, its secret is in the form, so nothing may cache the page."""
    response = templates.TemplateResponse(
        request, "auth_signup.html", {"request": request, "mail_on": mailer.mail_enabled(), **context}
    )
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
                {"request": request, "info": _(INVITE_UNUSABLE), "registration_open": await registration_allowed()},
                headers=_NO_STORE,
            )
        return _signup_response(
            request, invite=invite, email=open_invite["email"], email_locked=bool(open_invite["email"])
        )

    if not await registration_allowed():
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"request": request, "info": _("Registration is closed on this server."), "registration_open": False},
        )

    if await signed_in_user(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)

    return templates.TemplateResponse(request, "auth_signup.html", {"request": request})


@router.post("/signup")
async def signup_submit(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    invite: str = Form(""),
    sign_in_method: str = Form("password"),
):
    if not invite and not await registration_allowed():
        return error_redirect(request, "/login", _("Registration is closed on this server."))

    email = email.strip().lower()
    display_name = display_name.strip()
    invite_email = ""
    open_invite = None
    if invite:
        open_invite = await get_open_invite(invite)
        if not open_invite:
            return error_redirect(request, "/login", _(INVITE_UNUSABLE))
        invite_email = open_invite["email"]
    link_only = sign_in_method == "link" and mailer.mail_enabled()

    def _render_error(error: str):
        return _signup_response(
            request,
            error=error,
            email=invite_email or email,
            display_name=display_name,
            invite=invite,
            email_locked=bool(invite_email),
        )

    if not valid_email(email):
        return _render_error(_("Please enter a valid email address."))

    if link_only and not (invite_email and invite_email == email):
        # No password (#168): an address nobody vouched for gets a link first, and
        # the account exists only once the link is opened. A typo creates nothing.
        if await get_user_by_email(email):
            return _render_error(_("An account with that email already exists."))
        if not invite and email in ADMIN_EMAILS and await count_users() > 0:
            return _render_error(_(RESERVED_ADDRESS))
        token = await create_sign_in_link(
            email,
            user_id=None,
            purpose="signup",
            invite_id=open_invite["id"] if open_invite else "",
            display_name=display_name,
            language=current_language(),
        )
        if token:
            mailer.schedule(
                mailer.render_mail(
                    "signup",
                    current_language(),
                    to=email,
                    link=f"{config.BASE_URL}/login/link?token={token}",
                    minutes=SIGN_IN_LINK_TTL_MINUTES,
                )
            )
        return await _login_page(request, "check_signup", email)

    if link_only:
        # The invite was made for this address and sent there: it is proven.
        password = None
    elif len(password) < 8:
        return _render_error(_("Password must be at least 8 characters."))

    if password is not None and password != password_confirm:
        return _render_error(_("Passwords do not match."))

    if invite:
        # The invite is checked and used up in the same transaction as the insert.
        status, user = await create_user_from_invite(invite, email, password, display_name)
        if status == "invalid":
            return error_redirect(request, "/login", _(INVITE_UNUSABLE))
        if status == "email_mismatch":
            return _render_error(_("This invite is for a different email address."))
        if status == "email_reserved":
            return _render_error(_(RESERVED_ADDRESS))
        if status == "email_taken":
            return _render_error(_("An account with that email already exists."))
    else:
        existing = await get_user_by_email(email)
        if existing:
            return _render_error(_("An account with that email already exists."))
        # Open registration verifies no address, and VIRGIL_ADMIN_EMAILS makes one an
        # admin: only the first account, the operator's, may claim such an address here.
        if email in ADMIN_EMAILS and await count_users() > 0:
            return _render_error(_(RESERVED_ADDRESS))

        # With registration closed we're only here via the bootstrap-first-user
        # rule, so the INSERT itself enforces it and two concurrent first signups
        # can't both slip through the count check.
        user = await create_user(email, password, display_name, only_if_first=not REGISTRATION_OPEN)
        if user is None:
            return error_redirect(request, "/login", _("Registration is closed on this server."))

    response = await _start_account(request, user, used_invite=bool(invite), language=current_language())
    return response or _render_error(_("Could not create your account. Please try again."))


async def _start_account(request: Request, user: dict, *, used_invite: bool, language: str):
    """Provision a new account's database and sign it in, or None after rolling
    the account back when provisioning failed."""
    try:
        await create_user_db(user["db_filename"])
    except Exception:
        # Never leave an account whose database was never provisioned: it
        # could log in but every request would fail, and the email would be
        # permanently burned.
        from app.central_db import delete_user

        logger.exception("User DB provisioning failed for %s, rolling back account", user["email"])
        if used_invite:
            await reopen_invite_used_by(user["id"])
        await delete_user(user["id"])
        return None

    await _event(request, "invite_consume" if used_invite else "signup", user["id"])
    await update_user(user["id"], language=normalize_language(language))
    token = create_session(user["id"], user["session_epoch"])
    target = feedback_url("/onboarding", msg=_("Account created."))
    response = RedirectResponse(target, status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    response.headers.append("set-cookie", language_chosen_cookie_header(clear=True))
    return response


# --- Password reset (an admin hands over the link; there is no mailer) ---


def _reset_response(request: Request, **context):
    """The reset form carries the link's secret, so nothing may cache it."""
    return templates.TemplateResponse(request, "auth_reset.html", {"request": request, **context}, headers=_NO_STORE)


@router.get("/reset", response_class=HTMLResponse)
async def reset_page(request: Request, token: str = ""):
    reset = await get_open_reset(token)
    if not reset:
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"request": request, "info": _(RESET_UNUSABLE), "registration_open": await registration_allowed()},
            headers=_NO_STORE,
        )
    return _reset_response(request, token=token, email=reset["email"])


@router.post("/reset")
async def reset_submit(
    request: Request,
    token: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
):
    reset = await get_open_reset(token)
    if not reset:
        return error_redirect(request, "/login", _(RESET_UNUSABLE))
    if len(password) < 8:
        return _reset_response(
            request, token=token, email=reset["email"], error=_("Password must be at least 8 characters.")
        )
    if password != password_confirm:
        return _reset_response(request, token=token, email=reset["email"], error=_("Passwords do not match."))
    user_id = await complete_password_reset(token, password)
    if not user_id:
        return error_redirect(request, "/login", _(RESET_UNUSABLE))
    await _event(request, "password_reset", user_id)
    return success_redirect(request, "/login", _("Password set. Sign in with the new one."))


# --- Login ---


async def _login_page(request: Request, step: str = "email", email: str = "", **context):
    """The sign-in page at one of its steps: "email", "password" or "check_email"."""
    return templates.TemplateResponse(
        request,
        "auth_login.html",
        {
            "request": request,
            "step": step,
            "email": email,
            "mail_on": mailer.mail_enabled(),
            "registration_open": await registration_allowed(),
            **context,
        },
    )


async def _send_sign_in_link(request: Request, user: dict) -> None:
    """Email a one-time sign-in link, in the account's language, after the response.

    Past the per-address limit nothing is sent; the page reads the same either way.
    """
    token = await create_sign_in_link(user["email"], user_id=user["id"], language=user["language"])
    if not token:
        return
    mail = mailer.render_mail(
        "sign_in",
        user["language"],
        to=user["email"],
        link=f"{config.BASE_URL}/login/link?token={token}",
        minutes=SIGN_IN_LINK_TTL_MINUTES,
    )
    mailer.schedule(mail)
    await _event(request, "link_request", user["id"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if await signed_in_user(request.cookies.get(SESSION_COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return await _login_page(request)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(""),
):
    """Email first (#168). Without a password, the next step is the password for
    an account that has one, and an emailed link for any other. An unknown
    address gets the same "check your email" page and no mail, so the page does
    not say which addresses have link-only accounts. With mail off there are no
    links: every address gets the password step, as before."""
    email = username.strip().lower()

    if not password:
        if not mailer.mail_enabled():
            return await _login_page(request, "password", email)
        user = await get_user_by_email(email)
        if user and user["is_active"] and user["password_hash"]:
            return await _login_page(request, "password", email)
        if user and user["is_active"]:
            await _send_sign_in_link(request, user)
        return await _login_page(request, "check_email", email)

    user = await get_user_by_email(email)

    # Burn a bcrypt verify even for unknown emails, and for accounts without a
    # password, so timing stays uniform. An empty hash is never a match.
    password_hash = user["password_hash"] if user and user["password_hash"] else _DUMMY_PASSWORD_HASH
    password_ok = await verify_password_async(password, password_hash) and bool(user and user["password_hash"])

    if not user or not password_ok:
        await _event(
            request, "login_fail", user["id"] if user else None, "wrong password" if user else "unknown account"
        )
        return await _login_page(request, "password", email, error=_("Invalid email or password."))

    if not user["is_active"]:
        await _event(request, "login_fail", user["id"], "account disabled")
        return await _login_page(request, "password", email, error=_("This account has been deactivated."))

    # If MFA is enabled, redirect to MFA verification
    if user["totp_enabled"]:
        token = create_mfa_pending_session(user["id"])
        response = RedirectResponse("/mfa/verify", status_code=303)
        response.headers["set-cookie"] = session_cookie_header(token)
        return response

    # No MFA — create full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    await _event(request, "login_ok", user["id"])
    return await _signed_in_response(request, user)


@router.post("/login/link")
async def login_link_request(request: Request, username: str = Form("")):
    """ "Email me a sign-in link instead", from the password step."""
    email = username.strip().lower()
    if not mailer.mail_enabled():
        return await _login_page(request, "password", email)
    user = await get_user_by_email(email)
    if user and user["is_active"]:
        await _send_sign_in_link(request, user)
    return await _login_page(request, "check_email", email)


@router.get("/login/link", response_class=HTMLResponse)
async def login_link_page(request: Request, token: str = ""):
    """The page an emailed link opens. It signs nothing in by itself: mail apps
    that open links to scan them would use the link up before its owner."""
    if not await get_open_sign_in_link(token):
        return error_redirect(request, "/login", _(LINK_UNUSABLE))
    response = await _login_page(request, "confirm_link", token=token)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/login/link/confirm")
async def login_link_confirm(request: Request, token: str = Form("")):
    link = await use_sign_in_link(token)
    if not link:
        return error_redirect(request, "/login", _(LINK_UNUSABLE))
    if link["purpose"] == "signup":
        return await _finish_link_signup(request, link)
    user = await get_user_by_id(link["user_id"])
    if not user or not user["is_active"]:
        return error_redirect(request, "/login", _(LINK_UNUSABLE))
    await _event(request, "link_sign_in", user["id"])
    if user["totp_enabled"]:
        response = RedirectResponse("/mfa/verify", status_code=303)
        response.headers["set-cookie"] = session_cookie_header(create_mfa_pending_session(user["id"]))
        return response
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    await _event(request, "login_ok", user["id"], "with a sign-in link")
    return await _signed_in_response(request, user)


async def _finish_link_signup(request: Request, link: dict):
    """A signup link was opened, so its address is proven: create the account
    now, re-checking everything that may have changed since it was sent."""
    email = link["email"]
    if await get_user_by_email(email):
        return error_redirect(request, "/login", _("An account with that email already exists."))
    if link["invite_id"]:
        status, user = await create_user_from_invite("", email, None, link["display_name"], invite_id=link["invite_id"])
        if status != "created":
            return error_redirect(request, "/login", _(INVITE_UNUSABLE))
    else:
        if not await registration_allowed():
            return error_redirect(request, "/login", _("Registration is closed on this server."))
        if email in ADMIN_EMAILS and await count_users() > 0:
            return error_redirect(request, "/login", _(RESERVED_ADDRESS))
        user = await create_user(email, None, link["display_name"], only_if_first=not REGISTRATION_OPEN)
        if user is None:
            return error_redirect(request, "/login", _("Registration is closed on this server."))
    set_language(normalize_language(link["language"]))
    response = await _start_account(request, user, used_invite=bool(link["invite_id"]), language=link["language"])
    return response or error_redirect(request, "/login", _("Could not create your account. Please try again."))


async def _signed_in_response(request: Request, user: dict) -> RedirectResponse:
    """A full session for an account that has passed every check, and the dashboard."""
    token = create_session(user["id"], user["session_epoch"])
    language = await _signed_in_language(request, user)
    set_language(language)
    response = RedirectResponse(feedback_url("/", msg=_("Signed in.")), status_code=303)
    response.headers["set-cookie"] = session_cookie_header(token)
    response.headers.append("set-cookie", language_cookie_header(language))
    response.headers.append("set-cookie", language_chosen_cookie_header(clear=True))
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
    response = RedirectResponse(feedback_url("/login", msg=_("Signed out.")), status_code=303)
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
        return error_redirect(request, "/settings/mfa", _("MFA setup is unavailable. Start setup again."))
    # Without a password, the authenticator becomes the account's proof of
    # identity (#168), so a session alone may not enroll one.
    if not user["password_hash"] and not _fresh(request):
        return error_redirect(request, "/settings/mfa", _(SIGN_IN_AGAIN))

    secret = _totp_secret_plain(user["totp_secret"])
    totp = pyotp.TOTP(secret)
    if not totp.verify(totp_code, valid_window=1):
        return _mfa_setup_response(
            request,
            mfa_enabled=False,
            secret=secret,
            error=_("Invalid code. Please try again."),
        )

    # Re-writing the secret lazily migrates legacy plaintext rows to encrypted.
    await update_user(user["id"], totp_enabled=1, totp_secret=_totp_secret_stored(secret))
    await _event(request, "mfa_enable", user["id"])
    return await _recovery_codes_response(request, user, await bump_session_epoch(user["id"]))


async def _recovery_codes_response(request: Request, user: dict, epoch: int | None = None):
    """A new set of recovery codes, shown once (#168). With an epoch, every other
    session is signed out and this browser gets a fresh one."""
    codes = await replace_recovery_codes(user["id"])
    await _event(request, "recovery_codes_create", user["id"])
    response = templates.TemplateResponse(
        request, "auth_recovery_codes.html", {"request": request, "codes": codes}, headers=_NO_STORE
    )
    if epoch is not None:
        response.headers.append("set-cookie", session_cookie_header(create_session(user["id"], epoch)))
    return response


async def _second_factor_ok(request: Request, user: dict, code: str) -> bool:
    """A current authenticator code, or an unused recovery code (which it uses up).

    Every guess counts toward the same per-account lockout as /mfa/verify, so a
    session left open is no faster a way to guess six digits than the sign-in.
    """
    code = code.strip()
    if not code or not user["totp_enabled"]:
        return False
    now = datetime.now(UTC)
    if not await claim_mfa_attempt(user["id"], now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS):
        await _event(request, "mfa_lock", user["id"])
        return False
    ok = pyotp.TOTP(_totp_secret_plain(user["totp_secret"])).verify(code, valid_window=1)
    if not ok:
        ok = await use_recovery_code(user["id"], code)
    await finish_mfa_attempt(user["id"], ok, now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS)
    if not ok:
        await _event(request, "mfa_fail", user["id"])
    return ok


SIGN_IN_AGAIN = N_("For this, sign in again with a new link first.")


def _fresh(request: Request) -> bool:
    return session_is_fresh(request.cookies.get(SESSION_COOKIE, ""))


async def confirm_identity(request: Request, user: dict, password: str = "", code: str = "") -> str | None:
    """Re-check who is at the keyboard before something a session alone must not
    do (a token, a factory reset). None when confirmed, else the message to show.

    An account with a password confirms with it. One without (#168) confirms
    with its second factor, or, without one, with a sign-in from a link minutes
    ago: the inbox is its proof."""
    if user["password_hash"]:
        # verify_password raises on empty input, so an empty one is a wrong password.
        if password and await verify_password_async(password, user["password_hash"]):
            return None
        return _("Password is incorrect.")
    if not user["totp_enabled"]:
        return None if _fresh(request) else _(SIGN_IN_AGAIN)
    if await _second_factor_ok(request, user, code):
        return None
    return _("Invalid code. Please try again.")


@router.post("/settings/mfa/recovery-codes")
async def regenerate_recovery_codes(request: Request, totp_code: str = Form("")):
    """New codes, confirmed with the second factor itself: an account without a
    password has nothing else to confirm with."""
    user = getattr(request.state, "user", None)
    if not user or not user["totp_enabled"]:
        return error_redirect(request, SECURITY, _("Two-factor authentication is not on."))
    if not await _second_factor_ok(request, user, totp_code):
        return error_redirect(request, SECURITY, _("Invalid code. Please try again."))
    return await _recovery_codes_response(request, user)


@router.post("/settings/mfa/disable")
async def mfa_disable(request: Request, password: str = Form(""), totp_code: str = Form("")):
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/settings", _("MFA could not be disabled."))

    if user["password_hash"]:
        # verify_password raises on empty input, so an empty one is a wrong password.
        if not password or not await verify_password_async(password, user["password_hash"]):
            return _mfa_setup_response(request, mfa_enabled=True, error=_("Invalid password."))
    elif not await _second_factor_ok(request, user, totp_code):
        # No password to confirm with: the second factor confirms its own removal.
        return _mfa_setup_response(request, mfa_enabled=True, error=_("Invalid code. Please try again."))

    await update_user(user["id"], totp_enabled=0, totp_secret="")
    await delete_recovery_codes(user["id"])
    await _event(request, "mfa_disable", user["id"])
    return _resign(request, user["id"], await bump_session_epoch(user["id"]), _("MFA disabled."))


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
        return error_redirect(request, "/login", _("Sign in again to change your password."))
    # verify_password raises on empty input, so an empty one is a wrong password,
    # and an account without a password (#168) has none to change.
    if (
        not user["password_hash"]
        or not current_password
        or not await verify_password_async(current_password, user["password_hash"])
    ):
        return error_redirect(request, SECURITY, _("Current password is incorrect."))
    if len(new_password) < 8:
        return error_redirect(request, SECURITY, _("New password must be at least 8 characters."))
    if new_password != new_password_confirm:
        return error_redirect(request, SECURITY, _("New passwords do not match."))

    await update_user(user["id"], password_hash=await hash_password_async(new_password))
    epoch = await bump_session_epoch(user["id"])
    await _event(request, "password_change", user["id"])
    return _resign(request, user["id"], epoch, _("Password changed. Other devices are signed out."), SECURITY)


@router.post("/settings/password/add")
async def add_password(
    request: Request,
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
    totp_code: str = Form(""),
):
    """A link-only account (#168) sets a password. With two-factor on, the code
    confirms it; with "require TOTP" on, the gate then asks to set it up."""
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/login", _("Sign in again to change your password."))
    if user["password_hash"]:
        return error_redirect(request, SECURITY, _("This account already has a password."))
    # A session alone must not become a lasting login: the second factor, or a
    # sign-in from a link minutes ago, proves it is the owner.
    if user["totp_enabled"]:
        if not await _second_factor_ok(request, user, totp_code):
            return error_redirect(request, SECURITY, _("Invalid code. Please try again."))
    elif not _fresh(request):
        return error_redirect(request, SECURITY, _(SIGN_IN_AGAIN))
    if len(new_password) < 8:
        return error_redirect(request, SECURITY, _("New password must be at least 8 characters."))
    if new_password != new_password_confirm:
        return error_redirect(request, SECURITY, _("New passwords do not match."))
    await update_user(user["id"], password_hash=await hash_password_async(new_password))
    epoch = await bump_session_epoch(user["id"])
    await _event(request, "password_add", user["id"])
    return _resign(request, user["id"], epoch, _("Password set. Other devices are signed out."), SECURITY)


@router.post("/settings/password/remove")
async def remove_password(request: Request, current_password: str = Form("")):
    """Sign in with emailed links only (#168). Needs mail, or the account could
    never sign in again."""
    user = getattr(request.state, "user", None)
    if not user:
        return error_redirect(request, "/login", _("Sign in again to change your password."))
    if not mailer.mail_enabled():
        return error_redirect(request, SECURITY, _("This server cannot send sign-in links, so the password stays."))
    if (
        not user["password_hash"]
        or not current_password
        or not await verify_password_async(current_password, user["password_hash"])
    ):
        return error_redirect(request, SECURITY, _("Current password is incorrect."))
    await update_user(user["id"], password_hash="")
    epoch = await bump_session_epoch(user["id"])
    await _event(request, "password_remove", user["id"])
    return _resign(request, user["id"], epoch, _("Password removed. You sign in with emailed links now."), SECURITY)


# --- MFA Verify (during login) ---


@router.get("/mfa/verify", response_class=HTMLResponse)
async def mfa_verify_page(request: Request):
    if not mfa_pending_user_id(request.cookies.get(SESSION_COOKIE, "")):
        return error_redirect(request, "/login", _("MFA session expired. Sign in again."))
    return templates.TemplateResponse(request, "auth_mfa_verify.html", {"request": request})


@router.post("/mfa/verify")
async def mfa_verify_submit(request: Request, totp_code: str = Form(""), recovery_code: str = Form("")):
    user_id = mfa_pending_user_id(request.cookies.get(SESSION_COOKIE, ""))
    if not user_id:
        return error_redirect(request, "/login", _("MFA session expired. Sign in again."))

    user = await get_user_by_id(user_id)
    if not user or not user["is_active"]:
        return error_redirect(request, "/login", _("MFA session expired. Sign in again."))

    now = datetime.now(UTC)
    if not await claim_mfa_attempt(user["id"], now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS):
        await _event(request, "mfa_lock", user["id"])
        return templates.TemplateResponse(
            request,
            "auth_mfa_verify.html",
            {
                "request": request,
                "error": ngettext(
                    "Too many invalid codes. Try again in {num} minute.",
                    "Too many invalid codes. Try again in {num} minutes.",
                    MFA_LOCK_SECONDS // 60,
                ).format(num=MFA_LOCK_SECONDS // 60),
            },
            status_code=429,
        )

    # A recovery code stands in for the authenticator once (#168); a wrong one
    # counts toward the same lockout as a wrong six-digit code.
    if recovery_code.strip():
        code_ok = await use_recovery_code(user["id"], recovery_code)
        if code_ok:
            await _event(request, "recovery_code_use", user["id"])
    else:
        code_ok = pyotp.TOTP(_totp_secret_plain(user["totp_secret"])).verify(totp_code, valid_window=1)
    await finish_mfa_attempt(user["id"], code_ok, now, MFA_MAX_ATTEMPTS, MFA_LOCK_SECONDS)
    if not code_ok:
        await _event(request, "mfa_fail", user["id"])
        return templates.TemplateResponse(
            request,
            "auth_mfa_verify.html",
            {"request": request, "error": _("Invalid code. Please try again.")},
        )

    # MFA verified — upgrade to full session and record login time
    await update_user(user["id"], last_login_at=datetime.now(UTC).isoformat())
    await _event(request, "login_ok", user["id"], "with MFA")
    return await _signed_in_response(request, user)


# --- Interface language (#152) ---


async def _signed_in_language(request: Request, user: dict) -> str:
    """The language a sign-in continues in: one picked on the sign-in pages wins
    and the account keeps it; otherwise the account's own."""
    if request.cookies.get(LANGUAGE_CHOSEN_COOKIE):
        chosen = normalize_language(request.cookies.get(LANGUAGE_COOKIE))
        if chosen != user["language"]:
            await update_user(user["id"], language=chosen)
        return chosen
    return normalize_language(user["language"])


@router.post("/language")
async def choose_language(request: Request, language: str = Form(""), next: str = Form("/")):
    """Switch between English and Polish, before or after sign-in.

    A public path, so a signed-in account is found from its session cookie here.
    """
    chosen = normalize_language(language)
    user = await signed_in_user(request.cookies.get(SESSION_COOKIE, ""))
    if user:
        await update_user(user["id"], language=chosen)
    set_language(chosen)
    try:
        response = success_redirect(request, next, _("Language changed."))
    except ValueError:
        response = success_redirect(request, "/", _("Language changed."))
    response.headers.append("set-cookie", language_cookie_header(chosen))
    response.headers.append("set-cookie", language_chosen_cookie_header(clear=bool(user)))
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
