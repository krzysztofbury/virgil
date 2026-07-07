"""generate-andy must surface LLM failures, not swallow them; and the JSON parser
must tolerate the real prod failure mode (model wraps JSON in prose/fences).

Regression: the handler caught every exception and redirected to empty fields, so a
failing/misconfigured LLM looked like "request fires, nothing fills". Prod log showed
the real cause was a JSONDecodeError in parse_andy_response, not an auth error.
"""

import pytest
from conftest import csrf_token

from app.services.llm import parse_andy_response


def test_parse_plain_json():
    assert parse_andy_response('{"andy_body_desc": "x"}') == {"andy_body_desc": "x"}


def test_parse_fenced_json():
    assert parse_andy_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_prose_wrapped_json():
    # the real prod failure mode: model adds prose/reasoning around the object
    assert parse_andy_response('Sure, here you go:\n{"a": 1, "b": 2}\nHope that helps!') == {"a": 1, "b": 2}


def test_parse_doubled_closing_brace():
    # exact prod failure: model emitted a valid object then a spurious extra '}'
    assert parse_andy_response('{"andy_body_desc": "x"}\n}\n') == {"andy_body_desc": "x"}


@pytest.mark.parametrize("bad", ["", "   \n  ", "no json here at all"])
def test_parse_rejects_non_json(bad):
    with pytest.raises(ValueError):
        parse_andy_response(bad)


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
