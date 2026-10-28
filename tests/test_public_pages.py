"""The privacy policy, the terms and Plausible on the public pages (#179)."""

import pytest

from app import config

SCRIPT = "https://plausible.io/js/pa-test.js"
PLAUSIBLE = "https://plausible.io"


def _directives(policy: str) -> dict[str, list[str]]:
    """Each CSP directive's sources, compared as whole tokens."""
    parsed = {}
    for directive in policy.split(";"):
        name, *sources = directive.split()
        parsed[name] = sources
    return parsed


@pytest.fixture
def public(client, monkeypatch):
    monkeypatch.setattr(config, "LANDING_PAGE", True)
    monkeypatch.setattr(config, "CONTACT_EMAIL", "contact@example.com")
    monkeypatch.setattr(config, "OPERATOR_NAME", "Example Operator")
    client.cookies.clear()
    return client


@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_the_legal_pages_are_public_and_name_the_operator_from_the_settings(public, path):
    response = public.get(path, follow_redirects=False)
    assert response.status_code == 200
    assert "Example Operator" in response.text


@pytest.mark.parametrize(("language", "heading"), [("en", "Privacy policy"), ("pl", "Polityka prywatności")])
def test_the_privacy_policy_follows_the_language(public, language, heading):
    public.cookies.set("virgil_lang", language)
    assert f"<h1>{heading}</h1>" in public.get("/privacy").text


def test_the_landing_page_links_both(public, auth_client):
    page = public.get("/").text
    assert 'href="/privacy"' in page and 'href="/terms"' in page


def test_plausible_loads_on_public_pages_only_once_set(public, auth_client, monkeypatch):
    for path in ("/", "/privacy", "/terms"):
        assert "plausible" not in public.get(path).text, path

    monkeypatch.setattr(config, "PLAUSIBLE_SCRIPT_URL", SCRIPT)
    for path in ("/", "/privacy", "/terms"):
        response = public.get(path)
        assert f'src="{SCRIPT}"' in response.text, path
        directives = _directives(response.headers["content-security-policy"])
        assert PLAUSIBLE in directives["script-src"] and "'self'" in directives["script-src"]
        assert directives["connect-src"] == ["'self'", PLAUSIBLE]

    signed_in = auth_client.get("/").text
    assert SCRIPT not in signed_in and "plausible-init" not in signed_in
    assert SCRIPT not in auth_client.get("/settings").text


def test_the_privacy_policy_names_the_server_ai_provider_in_use(public, client, monkeypatch):
    from app.services import server_llm

    monkeypatch.setattr(config, "INTERNAL_LLM_MODEL", "xai/grok-4.7")
    monkeypatch.setattr(config, "INTERNAL_LLM_KEY", "env-key-0000")
    assert "currently xAI" in public.get("/privacy").text

    provider = client.portal.call(server_llm.add_provider, "openai", "openai/gpt-5", "openai-key-1111")
    try:
        client.portal.call(server_llm.activate_provider, provider)
        page = public.get("/privacy").text
        assert "currently OpenAI" in page and "currently xAI" not in page
    finally:
        client.portal.call(server_llm.delete_provider, provider)

    monkeypatch.setattr(config, "INTERNAL_LLM_KEY", "")
    assert (
        "currently" not in public.get("/privacy").text.split("model provider the operator chose")[1].split("</li>")[0]
    )
