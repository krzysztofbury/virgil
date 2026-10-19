"""Which request paths the API key authenticates, derived from the routes themselves.

The auth middleware lets these through without a session and the CSRF middleware
without a token (browsers cannot attach X-API-Key cross-site). Both used to keep
the same hand-written list of paths and regexes, which a new route could drift
out of. Here the list is every route that depends on api_db, plus /mcp, which
checks the key itself.
"""

import re
from functools import cache

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

# A parameter matches one path segment; an int one only ASCII digits. `\Z`, not
# `$`, so a trailing newline cannot sneak through.
_SEGMENT = "[^/]+"
_DIGITS = "[0-9]+"


def _depends_on(dependant: Dependant, call) -> bool:
    return any(dep.call is call or _depends_on(dep, call) for dep in dependant.dependencies)


def _pattern(route: APIRoute) -> re.Pattern:
    kinds = {param.name: param.field_info.annotation for param in route.dependant.path_params}
    parts = re.split(r"(\{\w+\})", route.path)
    regex = "".join(
        (_DIGITS if kinds.get(part[1:-1]) is int else _SEGMENT) if part.startswith("{") else re.escape(part)
        for part in parts
    )
    return re.compile(rf"^{regex}\Z")


@cache
def _api_key_patterns() -> tuple[re.Pattern, ...]:
    from app.mcp_endpoint import MCP_PATH
    from app.routers.api import api_db, router

    routes = [route for route in router.routes if isinstance(route, APIRoute) and _depends_on(route.dependant, api_db)]
    return (re.compile(rf"^{re.escape(MCP_PATH)}\Z"), *(_pattern(route) for route in routes))


def is_api_key_path(path: str) -> bool:
    """True when an X-API-Key route (REST or /mcp) owns this path."""
    return any(pattern.match(path) for pattern in _api_key_patterns())
