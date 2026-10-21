"""Scripts run only from /static or inline with the response's own nonce.

tests/test_browser_smoke.py proves the pages work under this policy in a
browser; these pin the policy and the nonces on the server side.
"""

import re

import pytest

SCRIPT = re.compile(r"<script\b([^>]*)>", re.I)
PAGES = ["/", "/daily", "/oura", "/bloodwork", "/settings", "/experiments/new", "/training", "/goals"]


def _directives(response) -> dict[str, list[str]]:
    policy = response.headers["content-security-policy"]
    return {part.split()[0]: part.split()[1:] for part in policy.split(";") if part.strip()}


def test_scripts_allow_neither_inline_code_nor_eval(auth_client):
    script_src = _directives(auth_client.get("/daily"))["script-src"]

    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src
    assert "'self'" in script_src
    assert [s for s in script_src if s.startswith("'nonce-")], "inline page scripts need a nonce"


def test_each_response_gets_its_own_nonce(auth_client):
    first = _directives(auth_client.get("/daily"))["script-src"]
    second = _directives(auth_client.get("/daily"))["script-src"]
    assert {s for s in first if s.startswith("'nonce-")} != {s for s in second if s.startswith("'nonce-")}


@pytest.mark.parametrize("path", PAGES)
def test_every_script_is_local_or_carries_this_responses_nonce(auth_client, path):
    response = auth_client.get(path)
    assert response.status_code == 200
    (nonce,) = [s[len("'nonce-") : -1] for s in _directives(response)["script-src"] if s.startswith("'nonce-")]

    for attributes in SCRIPT.findall(response.text):
        source = re.search(r'src="([^"]*)"', attributes)
        if source:
            assert source.group(1).startswith("/static/"), f"{path}: script from elsewhere: {source.group(1)}"
        else:
            assert f'nonce="{nonce}"' in attributes, f"{path}: an inline script without this response's nonce"
