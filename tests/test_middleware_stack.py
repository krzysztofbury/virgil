"""Middleware order: what an anonymous request costs, and which responses carry security headers.

The CSRF layer is the only one that reads a request body (up to 21 MB for
multipart). It used to be outermost, so an anonymous upload was buffered before
the rate limit or auth could refuse it, and security headers were innermost, so
the 403, 429, 503 and login redirects went out without CSP or X-Frame-Options.
"""

import asyncio

import pytest

from app.main import app


def _call(path: str, headers: list[tuple[bytes, bytes]], peer: str = "203.0.113.7"):
    """Drive the real ASGI stack once; record whether anything read the body."""
    body_read = False
    sent = []

    async def receive():
        nonlocal body_read
        body_read = True
        return {"type": "http.request", "body": b"_csrf_token=x&payload=" + b"a" * 1024, "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/x-www-form-urlencoded"), *headers],
        "client": (peer, 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], body_read


def test_a_post_without_a_csrf_cookie_is_refused_before_its_body_is_read(client):
    status, body_read = _call("/login", [])

    assert status == 403
    assert body_read is False


def test_an_anonymous_post_to_a_signed_in_page_is_redirected_before_its_body_is_read(client):
    status, body_read = _call("/settings/features", [(b"cookie", b"csrf_token=x")])

    assert status == 303
    assert body_read is False


def test_a_rate_limited_post_is_refused_before_its_body_is_read(client):
    import time

    import app.rate_limit as rate_limit

    peer = "203.0.113.8"
    rate_limit._buckets[f"{peer}:auth"] = [time.monotonic()] * rate_limit.AUTH_LIMIT

    status, body_read = _call("/login", [(b"cookie", b"csrf_token=x")], peer=peer)

    assert status == 429
    assert body_read is False


def _central_guard_tripped(client):
    app.state.central_migration_failure = True
    try:
        return client.get("/login", follow_redirects=False)
    finally:
        app.state.central_migration_failure = False


def _rate_limited(client):
    import app.rate_limit as rate_limit

    # Only sign-in form posts count; opening the page does not.
    for _ in range(rate_limit.AUTH_LIMIT):
        client.post("/login", follow_redirects=False)
    return client.post("/login", follow_redirects=False)


@pytest.mark.parametrize(
    ("make_response", "status"),
    [
        (lambda c: c.get("/", follow_redirects=False), 303),
        (lambda c: c.post("/login", data={"username": "a", "password": "b"}, follow_redirects=False), 403),
        (_rate_limited, 429),
        (_central_guard_tripped, 503),
    ],
    ids=["login-redirect", "csrf-403", "rate-limit-429", "central-guard-503"],
)
def test_responses_from_every_layer_carry_security_headers(client, make_response, status):
    client.cookies.clear()
    response = make_response(client)

    assert response.status_code == status
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
