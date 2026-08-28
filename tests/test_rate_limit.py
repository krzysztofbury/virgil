"""LLM-backed endpoints get a far tighter rate-limit bucket than general ones.

app/rate_limit.py:LLM_PATHS scopes /training/wod to LLM_LIMIT (10/min) instead of
GENERAL_LIMIT (120/min) — the only cost control on a paid endpoint. Mutating
LLM_PATHS to an empty frozenset leaves the full suite green (185 passed) because
nothing else in the suite fires 11 consecutive requests at this path.
"""

from conftest import csrf_token
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


def _rate_limited_client():
    from app.rate_limit import RateLimitMiddleware

    async def accepted(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/training/wod", accepted, methods=["POST"])])
    app.add_middleware(RateLimitMiddleware)
    return TestClient(app)


def test_direct_ingress_ignores_spoofed_cloudflare_ips(monkeypatch):
    import app.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "TRUST_CLOUDFLARE_HEADERS", False, raising=False)
    monkeypatch.setattr(rate_limit, "TRUSTED_PROXY_IPS", frozenset())
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_trusted_cloudflare_ingress_uses_valid_forwarded_ip(monkeypatch):
    import app.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "TRUST_CLOUDFLARE_HEADERS", True)
    monkeypatch.setattr(rate_limit, "TRUSTED_PROXY_IPS", frozenset({"testclient"}), raising=False)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses == [200] * 11


def test_trusted_cloudflare_ingress_rejects_invalid_forwarded_ip(monkeypatch):
    import app.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "TRUST_CLOUDFLARE_HEADERS", True)
    monkeypatch.setattr(rate_limit, "TRUSTED_PROXY_IPS", frozenset({"testclient"}), raising=False)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"spoofed-{i}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_trusted_cloudflare_mode_ignores_header_from_untrusted_peer(monkeypatch):
    import app.rate_limit as rate_limit

    monkeypatch.setattr(rate_limit, "TRUST_CLOUDFLARE_HEADERS", True)
    monkeypatch.setattr(rate_limit, "TRUSTED_PROXY_IPS", frozenset({"192.0.2.10"}), raising=False)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


def test_llm_path_rate_limited_while_general_path_stays_open(auth_client):
    """The 11th POST /training/wod within the window must 429; GET /training
    (general tier) must keep returning 200 throughout — proving the LLM bucket
    is scoped to its own path/key and doesn't also choke unrelated traffic.
    """
    token = csrf_token(auth_client, "/training")
    statuses = []
    for i in range(11):
        resp = auth_client.post(
            "/training/wod",
            data={
                "date": "2026-07-30",
                "duration_minutes": "60",
                "wod_text": f"rate limit probe {i}",
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        statuses.append(resp.status_code)

    # The success status (200 direct render, or 303 Post/Redirect/Get) is not
    # this test's concern — only that the rate limiter lets the first 10 through
    # and blocks the 11th.
    assert all(s < 400 for s in statuses[:10]), f"first 10 calls must be under the limit, got {statuses[:10]}"
    assert statuses[10] == 429, f"the 11th POST /training/wod must be rate-limited, got {statuses[10]}"

    assert auth_client.get("/training").status_code == 200, "the general tier must be unaffected by the LLM bucket"
