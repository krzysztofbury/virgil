import time

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# Sliding window rate limiter: per-IP, in-memory
# General endpoints: 120 req/min, auth endpoints: 10 req/min
GENERAL_LIMIT = 120
GENERAL_WINDOW_SECONDS = 60
AUTH_LIMIT = 10
AUTH_WINDOW_SECONDS = 60
AUTH_PATHS = frozenset({"/login", "/signup", "/setup", "/mfa/verify"})
MAX_BUCKETS = 10_000

# {ip: [(timestamp, ...), ...]}
_buckets: dict[str, list[float]] = {}


def _clean_bucket(bucket: list[float], window: int, now: float) -> list[float]:
    cutoff = now - window
    return [t for t in bucket if t > cutoff]


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        # Use CF-Connecting-IP when behind Cloudflare Tunnel, fall back to peer IP.
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "unknown")
        path = scope.get("path", "")
        now = time.monotonic()

        is_auth = path in AUTH_PATHS
        limit = AUTH_LIMIT if is_auth else GENERAL_LIMIT
        window = AUTH_WINDOW_SECONDS if is_auth else GENERAL_WINDOW_SECONDS
        key = f"{ip}:auth" if is_auth else ip

        bucket = _clean_bucket(_buckets.get(key, []), window, now)

        # Evict empty buckets to prevent unbounded memory growth
        if not bucket:
            _buckets.pop(key, None)
        else:
            _buckets[key] = bucket

        # Safety cap: evict oldest 25% of buckets when capacity exceeded.
        # Preserves rate-limit state for active abusers instead of nuking everything.
        if len(_buckets) > MAX_BUCKETS:
            evict_count = MAX_BUCKETS // 4
            keys_to_evict = sorted(
                _buckets.keys(),
                key=lambda k: _buckets[k][-1] if _buckets[k] else 0.0,
            )[:evict_count]
            for k in keys_to_evict:
                del _buckets[k]

        if len(bucket) >= limit:
            response = JSONResponse(
                {"detail": "Too many requests. Please try again later."},
                status_code=429,
                headers={"Retry-After": str(window)},
            )
            await response(scope, receive, send)
            return

        bucket.append(now)
        _buckets[key] = bucket

        await self.app(scope, receive, send)
