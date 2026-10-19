"""LLM-backed endpoints get a far tighter rate-limit bucket than general ones.

app/rate_limit.py:LLM_PATHS scopes /training/wod to LLM_LIMIT (10/min) instead of
GENERAL_LIMIT (120/min) — the only cost control on a paid endpoint. Mutating
LLM_PATHS to an empty frozenset leaves the full suite green (185 passed) because
nothing else in the suite fires 11 consecutive requests at this path.
"""

import logging
import sqlite3

import pytest
from conftest import csrf_token, user_db_path
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

# The tunnel container's address on the compose bridge network.
PROXY_PEER = "172.18.0.5"


def _rate_limited_client(peer: str = PROXY_PEER):
    from app.rate_limit import RateLimitMiddleware

    async def accepted(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/training/wod", accepted, methods=["POST"])])
    app.add_middleware(RateLimitMiddleware)
    return TestClient(app, client=(peer, 50000))


def _trust(monkeypatch, enabled: bool, proxies: str = ""):
    import app.rate_limit as rate_limit
    from app.config import parse_trusted_proxies

    monkeypatch.setattr(rate_limit, "TRUST_CLOUDFLARE_HEADERS", enabled)
    monkeypatch.setattr(rate_limit, "TRUSTED_PROXY_IPS", parse_trusted_proxies(proxies))
    monkeypatch.setattr(rate_limit, "_warned_untrusted_forwarding", False)


def test_direct_ingress_ignores_spoofed_cloudflare_ips(monkeypatch):
    _trust(monkeypatch, False)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


@pytest.mark.parametrize(
    ("proxies", "peer"),
    [
        (PROXY_PEER, PROXY_PEER),
        ("172.16.0.0/12", PROXY_PEER),
        ("bogus, 10.0.0.0/8, 172.18.0.0/16", PROXY_PEER),
        ("172.18.0.0/16", f"::ffff:{PROXY_PEER}"),
    ],
)
def test_trusted_cloudflare_ingress_uses_valid_forwarded_ip(monkeypatch, proxies, peer):
    """An exact address, or a range covering the Docker network, so a recreated
    tunnel container with a new address stays trusted."""
    _trust(monkeypatch, True, proxies)
    client = _rate_limited_client(peer)

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses == [200] * 11


def test_trusted_cloudflare_ingress_rejects_invalid_forwarded_ip(monkeypatch):
    _trust(monkeypatch, True, PROXY_PEER)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"spoofed-{i}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


@pytest.mark.parametrize("proxies", ["192.0.2.10", "10.0.0.0/8", "not-an-address", "0.0.0.0/0", "::/0"])
def test_trusted_cloudflare_mode_ignores_header_from_untrusted_peer(monkeypatch, proxies):
    _trust(monkeypatch, True, proxies)
    client = _rate_limited_client()

    statuses = [
        client.post("/training/wod", headers={"CF-Connecting-IP": f"198.51.100.{i + 1}"}).status_code for i in range(11)
    ]

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


@pytest.mark.parametrize(
    ("enabled", "proxies", "reason"),
    [(False, PROXY_PEER, "TRUST_CLOUDFLARE_HEADERS is off"), (True, "192.0.2.10", "not in VIRGIL_TRUSTED_PROXY_IPS")],
)
def test_an_untrusted_forwarded_ip_is_reported_once(monkeypatch, caplog, enabled, proxies, reason):
    """Behind the tunnel with trust off, every client shares one bucket. The log
    must say so the first time the header shows up, and not on every request."""
    _trust(monkeypatch, enabled, proxies)
    client = _rate_limited_client()

    with caplog.at_level(logging.WARNING, logger="app.rate_limit"):
        client.post("/training/wod")
        assert not caplog.records
        for _ in range(3):
            client.post("/training/wod", headers={"CF-Connecting-IP": "198.51.100.1"})

    assert len(caplog.records) == 1
    assert reason in caplog.records[0].getMessage()


def test_llm_path_rate_limited_while_general_path_stays_open(auth_client):
    """The 11th POST /training/wod within the window must 429; GET /training
    (general tier) must keep returning 200 throughout — proving the LLM bucket
    is scoped to its own path/key and doesn't also choke unrelated traffic.
    """
    token = csrf_token(auth_client, "/training")
    statuses = []
    try:
        _post_probes(auth_client, token, statuses)
    finally:
        # The captures queue a WOD parse, and a queued parse makes every later
        # capture in the suite skip its own: leave the shared database as found.
        conn = sqlite3.connect(user_db_path())
        conn.execute(
            "DELETE FROM jobs WHERE kind = 'wod_parse' AND json_extract(payload_json, '$.session_id') IN "
            "(SELECT id FROM training_sessions WHERE notes LIKE 'rate limit probe %')"
        )
        conn.execute("DELETE FROM training_sessions WHERE notes LIKE 'rate limit probe %'")
        conn.commit()
        conn.close()

    # The success status (200 direct render, or 303 Post/Redirect/Get) is not
    # this test's concern - only that the rate limiter lets the first 10 through
    # and blocks the 11th.
    assert all(s < 400 for s in statuses[:10]), f"first 10 calls must be under the limit, got {statuses[:10]}"
    assert statuses[10] == 429, f"the 11th POST /training/wod must be rate-limited, got {statuses[10]}"

    assert auth_client.get("/training").status_code == 200, "the general tier must be unaffected by the LLM bucket"


def _post_probes(auth_client, token, statuses):
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
