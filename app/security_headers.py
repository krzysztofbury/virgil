import re
import secrets

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import BASE_URL

# Every script, stylesheet and font is served from /static. The one outside
# origin is Cloudflare's Web Analytics beacon, which the proxy can inject.
BEACON_ORIGIN = "https://static.cloudflareinsights.com"
# A host and an optional port, nothing that could end or extend the policy.
_HOST = re.compile(r"[A-Za-z0-9.-]+(:[0-9]{1,5})?")


def _analytics_origin() -> str:
    """The origin of the Plausible script an admin set (#179), or ""."""
    from urllib.parse import urlsplit

    from app.server_settings import current

    parts = urlsplit(current.PLAUSIBLE_SCRIPT_URL)
    if parts.scheme != "https" or not _HOST.fullmatch(parts.netloc):
        return ""
    return f" https://{parts.netloc}"


def content_security_policy(nonce: str) -> str:
    """Scripts only from this origin or inline with this response's nonce.

    No 'unsafe-inline' and no 'unsafe-eval' for scripts: Alpine is its CSP build,
    and no template carries an inline event handler. Styles keep 'unsafe-inline'
    for the many style attributes and for what Chart.js and htmx set. With
    Plausible set up, its origin may serve its script and receive page views.
    """
    analytics = _analytics_origin()
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' {BEACON_ORIGIN}{analytics}; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data:; "
        f"connect-src 'self'{analytics}; "
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
