import logging

import bcrypt
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import BASE_URL

logger = logging.getLogger(__name__)

SESSION_COOKIE = "virgil_session"
SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 days
# Paths that don't require authentication
PUBLIC_PATHS = frozenset({"/login", "/setup", "/mfa/verify", "/offline", "/service-worker.js", "/api/oura/webhook"})
PUBLIC_PREFIXES = ("/static/", "/onboarding")

_signer: TimestampSigner | None = None
# Cache whether a user has been set up (avoids DB query on every request)
_user_exists: bool | None = None
_onboarding_done: bool | None = None


def _get_signer() -> TimestampSigner:
    global _signer
    if _signer is None:
        from app.services.encryption import get_signing_key

        _signer = TimestampSigner(get_signing_key())
    return _signer


def hash_password(password: str) -> str:
    # Use explicit raises instead of assert — these must survive python -O.
    if not password:
        raise ValueError("Cannot hash an empty password")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password:
        raise ValueError("Cannot verify an empty password")
    if not password_hash.startswith("$2"):
        raise ValueError("Invalid bcrypt hash format")
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session(username: str) -> str:
    """Create a signed session token."""
    return _get_signer().sign(username).decode()


def validate_session(token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> str | None:
    """Validate session token, return username or None."""
    try:
        return _get_signer().unsign(token, max_age=max_age).decode()
    except BadSignature:
        return None


def session_cookie_header(token: str) -> str:
    """Build Set-Cookie header value for the session."""
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict{secure}; Max-Age={SESSION_MAX_AGE_SECONDS}; Path=/"


def clear_session_cookie() -> str:
    """Build Set-Cookie header value that clears the session."""
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict{secure}; Max-Age=0; Path=/"


def mark_onboarding_complete():
    """Called after onboarding finishes to update the cached state."""
    global _onboarding_done
    _onboarding_done = True


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Check if any user exists — if not, redirect to setup (cached after first check)
        global _user_exists
        if _user_exists is None:
            from app.db import get_db

            db = await get_db()
            user_row = await db.execute_fetchall("SELECT id FROM auth_users WHERE id = 1")
            _user_exists = bool(user_row)
        if not _user_exists:
            response = RedirectResponse("/setup", status_code=303)
            await response(scope, receive, send)
            return

        # Check session cookie
        request = Request(scope)
        session_token = request.cookies.get(SESSION_COOKIE, "")
        username = validate_session(session_token) if session_token else None

        if not username or username.startswith("_mfa_pending:"):
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Store username in state for downstream use
        scope["state"] = {**scope.get("state", {}), "username": username}

        # Check if onboarding is completed — redirect to wizard if not.
        global _onboarding_done
        if _onboarding_done is not True:
            from app.db import get_db, get_setting

            db = await get_db()
            done = await get_setting(db, "onboarding_completed", "1")
            if done == "1":
                _onboarding_done = True
            else:
                if not path.startswith(("/onboarding", "/static/", "/api/", "/logout", "/service-worker")):
                    response = RedirectResponse("/onboarding", status_code=303)
                    await response(scope, receive, send)
                    return

        await self.app(scope, receive, send)
