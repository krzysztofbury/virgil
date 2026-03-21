import secrets
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import BASE_URL

CSRF_COOKIE = "csrf_token"
# ASGI normalizes all header names to lowercase, so this matches "X-CSRF-Token" sent by HTMX
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "_csrf_token"
SAFE_METHODS = frozenset({b"GET", b"HEAD", b"OPTIONS", b"TRACE"})
CSRF_EXEMPT_PATHS = frozenset({"/api/oura/webhook"})
# Hard cap on buffered form body size to prevent memory exhaustion.
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB


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
        if method not in SAFE_METHODS and path in CSRF_EXEMPT_PATHS:
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

                if "form" in content_type:
                    # Buffer the body, extract CSRF token, then replay it.
                    # Bounded to MAX_BODY_BYTES to prevent memory exhaustion.
                    body_chunks = []
                    total_size = 0
                    while True:
                        message = await receive()
                        chunk = message.get("body", b"")
                        total_size += len(chunk)
                        if total_size > MAX_BODY_BYTES:
                            response = Response("Request body too large", status_code=413)
                            await response(scope, receive, send)
                            return
                        body_chunks.append(chunk)
                        if not message.get("more_body", False):
                            break
                    body = b"".join(body_chunks)

                    parsed = parse_qs(body.decode(), keep_blank_values=True)
                    submitted = parsed.get(CSRF_FIELD, [""])[0]

                    # Create a new receive that replays the buffered body
                    body_sent = False

                    async def replay_receive() -> Message:
                        nonlocal body_sent
                        if not body_sent:
                            body_sent = True
                            return {"type": "http.request", "body": body, "more_body": False}
                        return {"type": "http.disconnect"}

                    receive = replay_receive

            if not cookie_token or not submitted or submitted != cookie_token:
                response = Response("CSRF validation failed", status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send_with_cookie)
