"""generate-andy must surface LLM failures, not swallow them.

Regression: the handler caught every exception and redirected to empty fields,
so a failing/misconfigured LLM looked like "request fires, nothing fills".
The test DB has no LLM provider, so call_llm raises naturally.
"""

from conftest import csrf_token


def test_generate_andy_surfaces_error(auth_client):
    token = csrf_token(auth_client, "/daily")
    resp = auth_client.post(
        "/daily/generate-andy",
        data={"date": "2026-07-07", "_csrf_token": token},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#andy-error", "error must retarget to the visible container"
    assert resp.headers.get("HX-Reswap") == "innerHTML"
    # Exact reason varies by env (no provider / bad key / bad model), but an LLM
    # error must be shown to the user, not swallowed into an empty redirect.
    assert "LLM" in resp.text and "⚠" in resp.text, f"reason must be shown, got: {resp.text[:200]}"
