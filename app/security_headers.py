from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import BASE_URL

# CDN origins used by the app
SCRIPT_ORIGINS = (
    "https://unpkg.com https://cdn.jsdelivr.net https://fonts.googleapis.com https://static.cloudflareinsights.com"
)
STYLE_ORIGINS = "https://cdn.jsdelivr.net https://fonts.googleapis.com https://fonts.gstatic.com"
FONT_ORIGINS = "https://fonts.gstatic.com"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-frame-options", b"DENY"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (
                            b"content-security-policy",
                            # Alpine.js requires unsafe-eval for x-data expressions.
                            f"default-src 'self'; "
                            f"script-src 'self' 'unsafe-inline' 'unsafe-eval' {SCRIPT_ORIGINS}; "
                            f"style-src 'self' 'unsafe-inline' {STYLE_ORIGINS}; "
                            f"font-src 'self' {FONT_ORIGINS}; "
                            f"img-src 'self' data:; "
                            f"connect-src 'self' {SCRIPT_ORIGINS} {FONT_ORIGINS}; "
                            f"manifest-src 'self'; "
                            f"worker-src 'self'".encode(),
                        ),
                    ]
                )
                # HSTS: enforce HTTPS when deployed behind TLS-terminating proxy.
                if BASE_URL.startswith("https"):
                    headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
