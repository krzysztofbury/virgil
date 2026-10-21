import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import BASE_URL

# Every script, stylesheet and font is served from /static. The one outside
# origin is Cloudflare's Web Analytics beacon, which the proxy can inject.
BEACON_ORIGIN = "https://static.cloudflareinsights.com"


def content_security_policy(nonce: str) -> str:
    """Scripts only from this origin or inline with this response's nonce.

    No 'unsafe-inline' and no 'unsafe-eval' for scripts: Alpine is its CSP build,
    and no template carries an inline event handler. Styles keep 'unsafe-inline'
    for the many style attributes and for what Chart.js and htmx set.
    """
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' {BEACON_ORIGIN}; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "manifest-src 'self'; "
        "worker-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # One per response: templates read it as request.state.csp_nonce.
        nonce = secrets.token_urlsafe(18)
        scope.setdefault("state", {})["csp_nonce"] = nonce

        async def send_with_headers(message: Message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"x-frame-options", b"DENY"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"strict-origin-when-cross-origin"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"content-security-policy", content_security_policy(nonce).encode()),
                    ]
                )
                # HSTS: enforce HTTPS when deployed behind TLS-terminating proxy.
                if BASE_URL.startswith("https"):
                    headers.append((b"strict-transport-security", b"max-age=63072000; includeSubDomains"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
