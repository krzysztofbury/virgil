"""Authentication middleware and session utilities for multi-user Virgil."""

import hashlib
import logging
import re

import bcrypt
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import BASE_URL

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
        # Inventory entry only (tests assert every API route is enumerated here);
        # runtime matching for this parametrized path happens via PUBLIC_PATTERNS.
        "/api/experiments/{experiment_id}/entries",
        "/api/training",
        "/api/training/detail",
        "/api/noporn",
    }
)
# /api/oura/webhook/{webhook_id} — per-user webhook callbacks authenticate via
# HMAC signature inside the handler, never via session cookies.
PUBLIC_PREFIXES = ("/static/", "/api/oura/webhook/")
# Parametrized API routes can't exact-match a request path. Anchored regexes,
# one numeric segment only — a broad prefix/suffix match would silently make
# any future /api/experiments/*/entries session route public. The route itself
# still enforces X-API-Key auth.
PUBLIC_PATTERNS = (re.compile(r"^/api/experiments/\d+/entries$"),)

BCRYPT_ROUNDS = 12

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_signer: TimestampSigner | None = None


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


def create_session(user_id: str) -> str:
    """Create a signed session token containing the user UUID."""
    return _get_signer().sign(user_id).decode()


def validate_session(token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> str | None:
    """Validate session token, return user UUID or None."""
    try:
        return _get_signer().unsign(token, max_age=max_age).decode()
    except BadSignature:
        return None


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


def mark_onboarding_complete():
    """No-op in multi-user mode — onboarding state is per-user DB."""
    pass


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
        session_token = request.cookies.get(SESSION_COOKIE, "")
        user_id = validate_session(session_token) if session_token else None

        if not user_id or user_id.startswith("_mfa_pending:"):
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Validate UUID format before DB lookup.
        if not _UUID_RE.match(user_id):
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Look up user in central DB.
        from app.central_db import get_user_by_id

        user = await get_user_by_id(user_id)

        if not user or not user["is_active"]:
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Open per-user database connection.
        from app.user_db import close_user_db, open_user_db

        user_db = await open_user_db(user["db_filename"])

        # Store user + DB + feature flags in request state.
        # Flags MUST be loaded here (not in a separate outer middleware): only here
        # is user_db guaranteed open, and this dict overwrites scope["state"] wholesale.
        from app.db import get_feature_flags

        scope["state"] = {
            **scope.get("state", {}),
            "username": user["email"],
            "user": user,
            "user_db": user_db,
            "features": await get_feature_flags(user_db),
        }

        try:
            # Check onboarding for this user.
            from app.db import get_setting

            done = await get_setting(user_db, "onboarding_completed", "0")
            if done != "1" and not path.startswith(("/onboarding", "/static/", "/api/", "/logout", "/service-worker")):
                response = RedirectResponse("/onboarding", status_code=303)
                await response(scope, receive, send)
                return

            await self.app(scope, receive, send)
        finally:
            await close_user_db(user_db)
