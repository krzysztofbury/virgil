"""Authentication middleware and session utilities for multi-user Virgil."""

import hashlib
import logging
import re

import bcrypt
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import BASE_URL
from app.validation import valid_uuid

logger = logging.getLogger(__name__)

SESSION_COOKIE = "virgil_session"
SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 days

# Paths that bypass auth entirely.
PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/signup",
        "/mfa/verify",
        "/offline",
        "/healthz",
        "/service-worker.js",
        "/api/oura/webhook",
        # REST API — each route enforces its own X-API-Key auth (app/routers/api.py).
        # Enumerated explicitly (no /api/ prefix whitelist) so future /api/* routes
        # default to session auth unless deliberately added here.
        "/api/summary",
        "/api/oura/today",
        "/api/habits",
        "/api/experiments/active",
        "/api/goal-areas",
        "/api/goals",
        "/api/goals/{goal_id}",
        "/api/goal-reps",
        "/api/goal-reps/{rep_id}",
        "/api/goal-reps/{rep_id}/transition",
        # Inventory entry only (tests assert every API route is enumerated here);
        # runtime matching for this parametrized path happens via PUBLIC_PATTERNS.
        "/api/experiments/{experiment_id}/entries",
        "/api/training",
        "/api/training/detail",
        "/api/noporn",
        "/api/freedom",
        # Inventory entries only; runtime matching for these happens via PUBLIC_PATTERNS.
        "/api/freedom/{habit}",
        "/api/freedom/{habit}/days/{day}",
        "/api/freedom/{habit}/bricks",
        "/api/freedom/bricks/{brick_id}",
        "/api/library",
        # Inventory entry only (tests assert every API route is enumerated here);
        # runtime matching for this parametrized path happens via PUBLIC_PATTERNS.
        "/api/library/{entry_id}",
        "/api/feniks/bricks",
        "/api/training/sessions",
        "/api/daily/{day}",
        "/api/feniks/daily/{day}",
        "/api/training/sessions/{session_id}",
        "/api/feniks/config",
        "/api/feniks/bricks/{brick_id}",
        "/api/measurements",
        "/api/measurements/{day}",
        # Hosted MCP endpoint (app/mcp_endpoint.py) - enforces the same X-API-Key itself.
        "/mcp",
    }
)
# /api/oura/webhook/{webhook_id} — per-user webhook callbacks authenticate via
# HMAC signature inside the handler, never via session cookies.
PUBLIC_PREFIXES = ("/static/", "/api/oura/webhook/")
# Parametrized API routes can't exact-match a request path. Anchored regexes,
# one numeric segment only — a broad prefix/suffix match would silently make
# any future /api/experiments/*/entries or /api/library/* session route public.
# The route itself still enforces X-API-Key auth.
# `\Z` (not `$`, which also matches just before a trailing "\n") and `[0-9]`
# (not `\d`, which is Unicode-aware and matches non-ASCII digits) so a path
# like "/api/library/1%0A" or "/api/library/١٢٣" can't sneak
# past the intended one-numeric-segment shape. Neither alias is exploitable
# here (the route still enforces X-API-Key auth either way), but the pattern
# is kept tight rather than half-hardened.
PUBLIC_PATTERNS = (
    re.compile(r"^/api/experiments/[0-9]+/entries\Z"),
    re.compile(r"^/api/library/[0-9]+\Z"),
    re.compile(r"^/api/goals/[0-9]+\Z"),
    re.compile(r"^/api/goal-reps/[0-9]+\Z"),
    re.compile(r"^/api/goal-reps/[0-9]+/transition\Z"),
    re.compile(r"^/api/training/sessions/[0-9]+\Z"),
    # One ISO calendar date; the route rejects anything that does not parse.
    re.compile(r"^/api/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"),
    re.compile(r"^/api/feniks/daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"),
    re.compile(r"^/api/feniks/bricks/[0-9]+\Z"),
    re.compile(r"^/api/measurements/[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"),
    # A habit slug is lowercase ASCII letters, digits and hyphens (habits.slugify).
    re.compile(r"^/api/freedom/[a-z0-9-]+\Z"),
    re.compile(r"^/api/freedom/[a-z0-9-]+/days/[0-9]{4}-[0-9]{2}-[0-9]{2}\Z"),
    re.compile(r"^/api/freedom/[a-z0-9-]+/bricks\Z"),
    re.compile(r"^/api/freedom/bricks/[0-9]+\Z"),
)

BCRYPT_ROUNDS = 12

_signer: TimestampSigner | None = None


def _login_response(scope, target: str = "/login") -> Response:
    is_htmx = any(name == b"hx-request" and value.lower() == b"true" for name, value in scope.get("headers", []))
    if is_htmx:
        return Response(status_code=200, headers={"HX-Redirect": target, "Cache-Control": "no-store"})
    return RedirectResponse(target, status_code=303)


def _stale_session_response(scope) -> Response:
    """Send a signed session whose account is gone or disabled to the login page
    and drop the cookie: /login turns away any validly signed session, so keeping
    it would bounce the browser between the two pages forever."""
    response = _login_response(scope)
    response.headers.append("set-cookie", clear_session_cookie())
    return response


def _get_signer() -> TimestampSigner:
    global _signer
    if _signer is None:
        from app.services.encryption import get_signing_key

        _signer = TimestampSigner(get_signing_key())
    return _signer


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Cannot hash an empty password")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    # Pre-hash with SHA-256 to avoid bcrypt's 72-byte truncation.
    prehashed = hashlib.sha256(password.encode()).hexdigest()
    return bcrypt.hashpw(prehashed.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password:
        raise ValueError("Cannot verify an empty password")
    if not password_hash.startswith("$2"):
        raise ValueError("Invalid bcrypt hash format")
    prehashed = hashlib.sha256(password.encode()).hexdigest()
    return bcrypt.checkpw(prehashed.encode(), password_hash.encode())


# A session carries the account's session epoch next to its UUID. Logout and MFA
# changes bump the epoch in the registry, which refuses every older session.
_EPOCH_SEPARATOR = "|"


def create_session(user_id: str, epoch: int) -> str:
    """Create a signed session token for the user UUID at this session epoch."""
    return _get_signer().sign(f"{user_id}{_EPOCH_SEPARATOR}{epoch}").decode()


def validate_session(token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> str | None:
    """Validate session token, return user UUID or None."""
    try:
        return _get_signer().unsign(token, max_age=max_age).decode()
    except BadSignature:
        return None


# A password-verified login waiting for its TOTP code. It only lets the browser
# reach /mfa/verify, so it lives minutes, not the full session lifetime.
MFA_PENDING_PREFIX = "_mfa_pending:"
MFA_PENDING_MAX_AGE_SECONDS = 600


def create_mfa_pending_session(user_id: str) -> str:
    """A password-verified login that may only reach /mfa/verify."""
    return _get_signer().sign(f"{MFA_PENDING_PREFIX}{user_id}").decode()


def session_identity(token: str) -> tuple[str, int] | None:
    """The user UUID and session epoch a cookie fully signs in, or None.

    A pending MFA login is not signed in: treating it as one sent /login to / and
    the middleware back again. A cookie from before epochs existed carries only the
    UUID and counts as epoch 0, so it lasts until the account's first bump.
    """
    value = validate_session(token) if token else None
    if not value or value.startswith(MFA_PENDING_PREFIX):
        return None
    user_id, separator, epoch = value.rpartition(_EPOCH_SEPARATOR)
    if not separator:
        return value, 0
    if not epoch.isascii() or not epoch.isdigit():
        return None
    return user_id, int(epoch)


async def signed_in_user(token: str) -> dict | None:
    """The active account a session cookie signs in at its current epoch, or None."""
    identity = session_identity(token)
    if not identity or not valid_uuid(identity[0]):
        return None
    from app.central_db import get_user_by_id

    user = await get_user_by_id(identity[0])
    if not user or not user["is_active"] or user["session_epoch"] != identity[1]:
        return None
    return user


def mfa_pending_user_id(token: str) -> str | None:
    """The user a still-fresh pending MFA login belongs to, or None."""
    value = validate_session(token, max_age=MFA_PENDING_MAX_AGE_SECONDS) if token else None
    return value.removeprefix(MFA_PENDING_PREFIX) if value and value.startswith(MFA_PENDING_PREFIX) else None


def session_cookie_header(token: str) -> str:
    """Build Set-Cookie header value for the session.

    SameSite=Lax (not Strict): OAuth providers (Oura) redirect back to
    /settings/oura/callback from a cross-site origin. Strict cookies are not
    sent on that top-level navigation, so the callback would bounce to /login
    and lose the authorization code. Lax still withholds the cookie on
    cross-site POSTs, and every state-changing route is CSRF-protected.
    """
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax{secure}; Max-Age={SESSION_MAX_AGE_SECONDS}; Path=/"


def clear_session_cookie() -> str:
    """Build Set-Cookie header value that clears the session."""
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax{secure}; Max-Age=0; Path=/"


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Allow public paths.
        if (
            path in PUBLIC_PATHS
            or any(path.startswith(p) for p in PUBLIC_PREFIXES)
            or any(p.match(path) for p in PUBLIC_PATTERNS)
        ):
            await self.app(scope, receive, send)
            return

        # Check session cookie — contains user UUID.
        request = Request(scope)
        identity = session_identity(request.cookies.get(SESSION_COOKIE, ""))
        if not identity:
            response = _login_response(scope)
            await response(scope, receive, send)
            return
        user_id, session_epoch = identity

        # Validate UUID format before DB lookup.
        if not valid_uuid(user_id):
            await _stale_session_response(scope)(scope, receive, send)
            return

        # Look up user in central DB.
        from app.central_db import get_user_by_id

        user = await get_user_by_id(user_id)

        if not user or not user["is_active"]:
            await _stale_session_response(scope)(scope, receive, send)
            return
        # A bumped epoch (logout, MFA change) revokes every session signed before it.
        # The cookie is left alone: a poll sent just before this browser turned MFA
        # on would otherwise clear the fresh cookie the MFA response set. /login
        # checks the epoch too, so the stale cookie cannot loop.
        if user["session_epoch"] != session_epoch:
            await _login_response(scope)(scope, receive, send)
            return

        # Open per-user database connection. Opening and every step after it
        # stay in this try so a failure (flag loading included) still closes it.
        from app.user_db import UserDatabaseMissing, close_user_db, open_user_db

        user_db = None
        try:
            try:
                user_db = await open_user_db(user["db_filename"])
            except UserDatabaseMissing:
                current = await get_user_by_id(user_id)
                if current and current["db_filename"] != user["db_filename"]:
                    # The row was read just before a factory reset swapped the file.
                    await _login_response(scope, "/")(scope, receive, send)
                    return
                # Gone for good (or the account was deleted meanwhile). Redirecting
                # to / would loop, since / opens the same missing file.
                logger.error("Database %s of account %s is missing", user["db_filename"], user_id)
                await Response(
                    "Your data could not be opened. Ask the administrator to restore it.",
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return

            # Store user + DB + feature flags in request state.
            # Flags MUST be loaded here (not in a separate outer middleware): only here
            # is user_db guaranteed open, and this dict overwrites scope["state"] wholesale.
            from app.db import get_feature_flags, get_setting

            scope["state"] = {
                **scope.get("state", {}),
                "username": user["email"],
                "user": user,
                "user_db": user_db,
                "features": await get_feature_flags(user_db),
            }

            # Check onboarding for this user.
            done = await get_setting(user_db, "onboarding_completed", "0")
            if done != "1" and not path.startswith(("/onboarding", "/static/", "/api/", "/logout", "/service-worker")):
                response = RedirectResponse("/onboarding", status_code=303)
                await response(scope, receive, send)
                return

            await self.app(scope, receive, send)
        finally:
            await close_user_db(user_db)
