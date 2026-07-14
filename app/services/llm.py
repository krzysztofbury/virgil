import json
import logging

import litellm

from app.services.encryption import decrypt

logger = logging.getLogger(__name__)

# Suppress litellm's verbose default logging.
litellm.suppress_debug_info = True


async def get_active_provider(db) -> dict | None:
    """Return the user's active LLM provider from the DB, or None."""
    rows = await db.execute_fetchall("SELECT * FROM llm_providers WHERE is_active = 1 LIMIT 1")
    return dict(rows[0]) if rows else None


async def _resolve_provider(db) -> tuple[str, str]:
    """Return (model_string, api_key). User provider first, internal fallback.

    Raises ValueError if no provider is available.
    """
    # 1. Try user-configured provider from DB.
    provider = await get_active_provider(db)
    if provider:
        api_key = decrypt(provider["api_key_enc"])
        model = provider["model"]
        if api_key and model:
            return model, api_key

    # 2. Fall back to internal provider from env vars.
    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

    if INTERNAL_LLM_KEY and INTERNAL_LLM_MODEL:
        return INTERNAL_LLM_MODEL, INTERNAL_LLM_KEY

    raise ValueError("No LLM provider available — configure one in Settings or set VIRGIL_INTERNAL_LLM_KEY")


async def llm_available(db) -> bool:
    """True if call_llm() would have a provider — user-configured OR the
    internal env fallback. Every UI/scheduler availability check must use this,
    not get_active_provider(), or internal-key-only deployments lose features.
    """
    try:
        await _resolve_provider(db)
        return True
    except ValueError:
        return False


async def call_llm(
    db,
    system_prompt: str,
    user_prompt: str,
    *,
    json_mode: bool = False,
    reasoning_effort: str | None = None,
    max_tokens: int = 2048,
) -> str:
    """Call an LLM using the resolved provider (user or internal fallback).

    json_mode=True asks the provider for a strict JSON object.
    reasoning_effort ('disable'|'low'|'medium'|'high') caps the model's thinking
    budget — litellm maps it to Gemini's thinking config ('disable' = 0 tokens).
    CAVEAT: with drop_params=True the flag is silently dropped for models
    litellm cannot map it for; those models think unbounded, eating max_tokens
    and truncating the answer — structured-task callers should therefore pass
    a generous max_tokens as well.
    Returns the assistant's text response.
    """
    assert max_tokens >= 1, f"max_tokens must be positive: {max_tokens}"
    assert max_tokens <= 65536, f"max_tokens beyond any provider cap: {max_tokens}"
    model, api_key = await _resolve_provider(db)

    kwargs: dict = {"drop_params": True}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    try:
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=api_key,
            max_tokens=max_tokens,
            timeout=60.0,
            **kwargs,
        )
    except litellm.AuthenticationError:
        raise ValueError(f"LLM authentication failed for model {model} — check your API key") from None
    except litellm.RateLimitError:
        raise ValueError(f"LLM rate limit exceeded for model {model} — try again later") from None
    except litellm.Timeout:
        raise ValueError(f"LLM request timed out for model {model}") from None
    except litellm.APIError as exc:
        raise ValueError(f"LLM API error for model {model}: {exc}") from exc

    choice = response.choices[0]
    finish = str(getattr(choice, "finish_reason", "") or "").lower()
    content = choice.message.content
    # Ground-truth diagnostics: finish reason + token usage (incl. reasoning tokens).
    logger.info("LLM %s finish=%s usage=%s", model, finish, getattr(response, "usage", None))
    if finish in {"length", "max_tokens", "maxtokens"}:
        if json_mode and content:
            # Truncated-but-present JSON is salvageable — the caller's parser
            # (parse_andy_response) is the designated repair layer. Raising
            # here would make that repair dead code for providers that label
            # truncation correctly.
            logger.warning(
                "LLM response truncated at %d tokens for %s — returning partial for repair", max_tokens, model
            )
            return content
        raise ValueError(
            f"LLM response truncated at {max_tokens}-token limit for {model} — raise cap or lower reasoning"
        )
    assert content is not None, f"LLM returned no text content (model={model}, finish={finish})"
    return content


def parse_andy_response(text: str) -> dict:
    """Extract a JSON object from an LLM response.

    Tolerates markdown code fences and surrounding prose/reasoning by falling back
    to the outermost {...}. Raises ValueError (with a snippet of what came back) if
    no JSON object can be parsed — so the failure is diagnosable, not a bare decode error.
    """
    if not text or not text.strip():
        raise ValueError("LLM returned an empty response")

    # raw_decode parses the FIRST complete JSON object starting at the first '{'
    # and ignores everything after it — tolerating code fences, leading prose, and
    # trailing junk like a doubled closing brace ('}\n}') that models sometimes emit.
    start = text.find("{")
    if start != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj

        # Truncated output (thinking ate the token budget mid-object) is the
        # dominant real-world failure — salvage it by closing the JSON instead
        # of throwing away four perfectly good suggestions.
        trimmed = text.rstrip().rstrip(",")
        for suffix in ("}", '"}'):
            try:
                obj, _ = json.JSONDecoder().raw_decode(trimmed + suffix, start)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj:
                logger.warning("Repaired truncated LLM JSON (len=%d, suffix=%r)", len(text), suffix)
                return obj

    # head + tail + length so the failure is self-diagnosing: ending in '}' means
    # complete-but-unparseable; ending mid-string means truncated.
    raise ValueError(f"LLM did not return a JSON object (len={len(text)}): {text[:120]!r}…{text[-80:]!r}")
