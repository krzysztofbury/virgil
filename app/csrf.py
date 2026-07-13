import logging
import secrets
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import BASE_URL

logger = logging.getLogger(__name__)

CSRF_COOKIE = "csrf_token"
# ASGI normalizes all header names to lowercase, so this matches "X-CSRF-Token" sent by HTMX
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "_csrf_token"
SAFE_METHODS = frozenset({b"GET", b"HEAD", b"OPTIONS", b"TRACE"})
CSRF_EXEMPT_PATHS = frozenset({"/api/oura/webhook"})
# Per-user webhook callbacks (/api/oura/webhook/{id}) authenticate via HMAC, not cookies.
CSRF_EXEMPT_PREFIXES = ("/api/oura/webhook/",)
# Hard caps on buffered form body size to prevent memory exhaustion.
# Multipart gets a higher cap: onboarding accepts medical PDFs up to 20 MB
# (app/routers/onboarding.py MAX_UPLOAD_BYTES) plus multipart framing overhead.
MAX_URLENCODED_BODY_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_MULTIPART_BODY_BYTES = 21 * 1024 * 1024  # 20 MB payload + framing


async def _extract_multipart_token(scope: Scope, receive: Receive) -> str:
    """Parse a buffered multipart body and return the CSRF form field.

    parse_qs() cannot parse multipart boundaries, so we delegate to Starlette's
    form parser (python-multipart) on a replayed copy of the body.
    """
    request = Request(scope, receive)
    try:
        form = await request.form()
        value = form.get(CSRF_FIELD, "")
        return value if isinstance(value, str) else ""
    except Exception:
        # Malformed multipart — treat as missing token; the 403 below applies.
        logger.warning("Could not parse multipart body for CSRF token", exc_info=True)
        return ""


class CSRFMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        csrf_token = request.cookies.get(CSRF_COOKIE)
        if not csrf_token:
            csrf_token = secrets.token_urlsafe(32)
        scope["state"] = {**scope.get("state", {}), "csrf_token": csrf_token}

        # Wrap send to set CSRF cookie on response
        async def send_with_cookie(message: Message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                secure = "; Secure" if BASE_URL.startswith("https") else ""
                cookie_val = f"{CSRF_COOKIE}={csrf_token}; HttpOnly; SameSite=Strict{secure}; Max-Age=86400; Path=/"
                headers.append((b"set-cookie", cookie_val.encode()))
                message = {**message, "headers": headers}
            await send(message)

        method = scope.get("method", "GET").encode()
        path = scope.get("path", "")
        if method not in SAFE_METHODS and (
            path in CSRF_EXEMPT_PATHS or any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send_with_cookie)
            return
        if method not in SAFE_METHODS:
            cookie_token = request.cookies.get(CSRF_COOKIE, "")

            # Check header first (HTMX)
            submitted = ""
            for header_name, header_value in scope.get("headers", []):
                if header_name == CSRF_HEADER.encode():
                    submitted = header_value.decode()
                    break

            if not submitted:
                content_type = ""
                for header_name, header_value in scope.get("headers", []):
                    if header_name == b"content-type":
                        content_type = header_value.decode()
                        break

                is_multipart = "multipart/form-data" in content_type
                if "form" in content_type:
                    # Buffer the body, extract CSRF token, then replay it.
                    # Bounded to prevent memory exhaustion.
                    max_bytes = MAX_MULTIPART_BODY_BYTES if is_multipart else MAX_URLENCODED_BODY_BYTES
                    body_chunks = []
                    total_size = 0
                    while True:
                        message = await receive()
                        chunk = message.get("body", b"")
                        total_size += len(chunk)
                        if total_size > max_bytes:
                            response = Response("Request body too large", status_code=413)
                            await response(scope, receive, send)
                            return
                        body_chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
                    body = b"".join(body_chunks)

                    def make_replay() -> Receive:
                        body_sent = False

                        async def replay_receive() -> Message:
                            nonlocal body_sent
                            if not body_sent:
                                body_sent = True
                                return {"type": "http.request", "body": body, "more_body": False}
                            return {"type": "http.disconnect"}

                        return replay_receive

                    if is_multipart:
                        # Consumes one replay; hand a fresh one to the app below.
                        submitted = await _extract_multipart_token(scope, make_replay())
                    else:
                        parsed = parse_qs(body.decode(), keep_blank_values=True)
                        submitted = parsed.get(CSRF_FIELD, [""])[0]

                    receive = make_replay()

            if not cookie_token or not submitted or not secrets.compare_digest(submitted, cookie_token):
                response = Response("CSRF validation failed", status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send_with_cookie)
