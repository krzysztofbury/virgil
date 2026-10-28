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

# Each parameter matches only its own shape, so a session route that one day
# shares a prefix (say /api/measurements/export) cannot fall inside an API-key
# pattern. `\Z`, not `$`, keeps a trailing newline out; `[0-9]`, not `\d`, keeps
# non-ASCII digits out.
_DIGITS = "[0-9]+"
_SHAPES = {
    "day": "[0-9]{4}-[0-9]{2}-[0-9]{2}",
    # A habit slug: lowercase ASCII letters, digits and hyphens (habits.slugify).
    "habit": "[a-z0-9-]+",
    # A feature flag name. Wider than the known names on purpose, so an unknown or
    # miscased one reaches the route and is refused there with the list of known ones.
    "feature": "[A-Za-z0-9_-]+",
}


def _depends_on(dependant: Dependant, call) -> bool:
    return any(dep.call is call or _depends_on(dep, call) for dep in dependant.dependencies)


def _pattern(route: APIRoute) -> re.Pattern:
    kinds = {param.name: param.field_info.annotation for param in route.dependant.path_params}

    def shape(name: str) -> str:
        if kinds.get(name) is int:
            return _DIGITS
        if name not in _SHAPES:
            raise RuntimeError(f"{route.path}: give the path parameter {name!r} a shape in app/api_paths.py")
        return _SHAPES[name]

    parts = re.split(r"(\{\w+\})", route.path)
    regex = "".join(shape(part[1:-1]) if part.startswith("{") else re.escape(part) for part in parts)
    return re.compile(rf"^{regex}\Z")


@cache
def _api_key_patterns() -> tuple[re.Pattern, ...]:
    from app.mcp_endpoint import MCP_PATH
    from app.routers.api import api_db, router

    if any(not isinstance(route, APIRoute) for route in router.routes):
        # An included sub-router would be skipped below and its key routes would
        # demand a session; fail loudly so this module learns to walk it.
        raise RuntimeError("app.routers.api has a nested router; teach app/api_paths.py to walk it")
    routes = [route for route in router.routes if _depends_on(route.dependant, api_db)]
    return (re.compile(rf"^{re.escape(MCP_PATH)}\Z"), *(_pattern(route) for route in routes))


def is_api_key_path(path: str) -> bool:
    """True when an X-API-Key route (REST or /mcp) owns this path."""
    return any(pattern.match(path) for pattern in _api_key_patterns())
