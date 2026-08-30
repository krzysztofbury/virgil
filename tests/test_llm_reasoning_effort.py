"""The thinking budget is a user setting, not a hardcoded value per call site.

The bug this closes: every structured caller pinned reasoning_effort='disable',
which Gemini accepts and OpenAI rejects outright with
"Unsupported value: 'reasoning_effort' does not support 'disable'".
"""

import asyncio
import sqlite3

import litellm
import pytest

from app.services.llm import (
    DEFAULT_REASONING_EFFORT,
    REASONING_EFFORT_SETTING,
    REASONING_EFFORTS,
    _portable_effort,
    call_llm,
    reasoning_effort_choice,
    resolve_reasoning_effort,
)
from tests.conftest import csrf_token, user_db_path


def _set_effort(value: str) -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)", (REASONING_EFFORT_SETTING, value))
        db.commit()
    finally:
        db.close()


def _clear_effort() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM app_settings WHERE key = ?", (REASONING_EFFORT_SETTING,))
        db.commit()
    finally:
        db.close()


class _FakeDB:
    def __init__(self, stored: str | None) -> None:
        self._stored = stored

    async def execute_fetchall(self, sql, params=()):
        if "app_settings" in sql:
            return [{"value": self._stored}] if self._stored is not None else []
        return [{"api_key_enc": "", "model": "gemini/gemini-3-pro-preview", "is_active": 1}]


def test_the_default_is_medium_and_never_a_value_openai_rejects():
    assert DEFAULT_REASONING_EFFORT == "medium"
    assert "disable" not in REASONING_EFFORTS
    assert "minimal" not in REASONING_EFFORTS
    assert REASONING_EFFORTS == ("none", "low", "medium", "high", "xhigh")


@pytest.mark.parametrize("stored", [None, "", "disable", "wildly-wrong", "MEDIUM"])
def test_an_unusable_stored_level_falls_back_to_the_default(stored):
    assert asyncio.run(resolve_reasoning_effort(_FakeDB(stored))) == DEFAULT_REASONING_EFFORT


@pytest.mark.parametrize("stored", list(REASONING_EFFORTS))
def test_a_stored_level_is_honoured(stored):
    assert asyncio.run(resolve_reasoning_effort(_FakeDB(stored))) == stored
    assert reasoning_effort_choice(stored) == stored


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai/gpt-5", "xhigh"),
        ("azure/gpt-5", "xhigh"),
        ("gemini/gemini-3-pro-preview", "high"),
        ("anthropic/claude-sonnet-4-20250514", "high"),
    ],
)
def test_xhigh_steps_down_for_providers_that_would_reject_it(model, expected):
    """drop_params removes unsupported PARAMETERS, never unsupported values."""
    assert _portable_effort("xhigh", model) == expected
    assert _portable_effort("medium", model) == "medium"


def test_every_offered_level_survives_the_litellm_boundary():
    from litellm.utils import get_optional_params

    for provider, model in (("openai", "gpt-4o-mini"), ("gemini", "gemini-3-pro-preview")):
        for effort in REASONING_EFFORTS:
            get_optional_params(
                model=model,
                custom_llm_provider=provider,
                max_tokens=2048,
                drop_params=True,
                reasoning_effort=_portable_effort(effort, f"{provider}/{model}"),
            )


def test_call_llm_always_sends_a_level_so_the_model_never_picks_its_own(monkeypatch):
    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        raise litellm.AuthenticationError("stop here", "gemini", "gemini/gemini-3-pro-preview")

    async def fake_provider(_db):
        return "gemini/gemini-3-pro-preview", "secret"

    monkeypatch.setattr("app.services.llm._resolve_provider", fake_provider)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    with pytest.raises(ValueError):
        asyncio.run(call_llm(_FakeDB("low"), "system", "user"))
    assert seen["reasoning_effort"] == "low"

    seen.clear()
    with pytest.raises(ValueError):
        asyncio.run(call_llm(_FakeDB("low"), "system", "user", reasoning_effort="high"))
    assert seen["reasoning_effort"] == "high", "an explicit per-call level must win over the setting"


def test_settings_page_offers_the_level_and_shows_the_stored_choice(auth_client):
    _set_effort("high")
    try:
        html = auth_client.get("/settings?tab=general").text
        assert 'action="/settings/llm/reasoning"' in html
        assert '<option value="high" selected>high</option>' in html
        assert 'value="disable"' not in html
    finally:
        _clear_effort()


def test_saving_the_level_accepts_the_allowlist_and_refuses_anything_else(auth_client):
    token = csrf_token(auth_client, "/settings?tab=general")
    try:
        ok = auth_client.post(
            "/settings/llm/reasoning",
            data={"reasoning_effort": "low", "_csrf_token": token},
            follow_redirects=False,
        )
        assert ok.status_code == 303
        assert "msg=" in ok.headers["location"]

        rejected = auth_client.post(
            "/settings/llm/reasoning",
            data={"reasoning_effort": "disable", "_csrf_token": token},
            follow_redirects=False,
        )
        assert rejected.status_code == 303
        assert "err=" in rejected.headers["location"]

        db = sqlite3.connect(user_db_path())
        try:
            stored = db.execute("SELECT value FROM app_settings WHERE key = ?", (REASONING_EFFORT_SETTING,)).fetchone()
        finally:
            db.close()
        assert stored == ("low",)
    finally:
        _clear_effort()


def test_no_call_site_pins_a_provider_specific_level():
    from pathlib import Path

    for path in Path("app").rglob("*.py"):
        if path.name == "llm.py":
            continue
        assert 'reasoning_effort="disable"' not in path.read_text(), path
