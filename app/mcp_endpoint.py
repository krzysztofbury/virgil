"""Hosted MCP endpoint: the stdio wrapper's tools served over Streamable HTTP at /mcp.

Clients connect to https://<host>/mcp with the same X-API-Key header as the REST API
(plus the Cloudflare Access service-token headers when the host sits behind Access),
so no local checkout, uv or launcher script is needed.

The tools are the ones in mcp_server/virgil_mcp.py, unchanged: each still calls the
REST API, here at the configured bind with the server's own key, so /mcp and /api
share one auth path, one user mapping and one set of validation rules.
"""

import ipaddress
from contextlib import asynccontextmanager

import httpx
from fastapi import HTTPException
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.config import API_KEY, HOST, PORT
from app.routers.api import require_api_key
from mcp_server import virgil_mcp

MCP_PATH = "/mcp"
MCP_METHODS = ["GET", "POST", "DELETE"]


def loopback_base_url(host: str, port: int) -> str:
    """URL of this process's REST API for hosted MCP tool calls.

    Wildcard binds (0.0.0.0, ::) are not destinations, so dial that family's
    loopback. Any other host is used as configured, with IPv6 literals bracketed.
    """
    literal = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        parsed = ipaddress.ip_address(literal)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.is_unspecified:
        literal = "::1" if parsed.version == 6 else "127.0.0.1"
    elif parsed is not None:
        literal = str(parsed)
    if ":" in literal:
        literal = f"[{literal}]"
    return f"http://{literal}:{port}"


# Process-wide: every tool call made inside the app goes to its own REST API.
virgil_mcp.use_client(
    httpx.Client(base_url=loopback_base_url(HOST, PORT), headers={"X-API-Key": API_KEY}, timeout=30.0)
)


class MCPEndpoint:
    """ASGI endpoint: refuse a missing or wrong X-API-Key, hand the rest to the MCP SDK."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            require_api_key(Request(scope).headers.get("x-api-key", ""))
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)
            return
        manager = getattr(scope["app"].state, "mcp_session_manager", None)
        if manager is None:
            await JSONResponse({"detail": "MCP endpoint not running"}, status_code=503)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)


@asynccontextmanager
async def mcp_session(app: Starlette):
    """Run an MCP session manager for one application lifespan.

    A manager can run only once, so each lifespan builds a fresh one and keeps it on
    its own app.state. Stateless JSON mode keeps every request self-contained: nothing
    to lose when the container restarts or the tunnel drops a long-lived stream.
    """
    virgil_mcp.mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # DNS rebinding protection guards unauthenticated localhost servers; this one
        # sits behind a public hostname and refuses any request without the API key.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = virgil_mcp.mcp.session_manager
    async with manager.run():
        app.state.mcp_session_manager = manager
        try:
            yield
        finally:
            app.state.mcp_session_manager = None
