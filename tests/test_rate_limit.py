"""LLM-backed endpoints get a far tighter rate-limit bucket than general ones.

app/rate_limit.py:LLM_PATHS scopes /training/wod to LLM_LIMIT (10/min) instead of
GENERAL_LIMIT (120/min) — the only cost control on a paid endpoint. Mutating
LLM_PATHS to an empty frozenset leaves the full suite green (185 passed) because
nothing else in the suite fires 11 consecutive requests at this path.
"""

from conftest import csrf_token


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
