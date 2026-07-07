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


async def call_llm(
    db, system_prompt: str, user_prompt: str, *, json_mode: bool = False, reasoning_effort: str | None = None
) -> str:
    """Call an LLM using the resolved provider (user or internal fallback).

    json_mode=True asks the provider for a strict JSON object.
    reasoning_effort ('disable'|'low'|'medium'|'high') caps the model's thinking
    budget — litellm maps it to Gemini's thinking config ('disable' = 0 tokens).
    For trivial structured tasks, unbounded thinking eats the token budget and
    truncates the actual answer, so we disable it rather than inflate max_tokens.
    (drop_params lets litellm skip either flag for models that don't support it.)
    Returns the assistant's text response.
    """
    model, api_key = await _resolve_provider(db)

    max_tokens = 2048

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
    # Ground-truth diagnostics: finish reason + token usage (incl. reasoning tokens).
    logger.info("LLM %s finish=%s usage=%s", model, finish, getattr(response, "usage", None))
    if finish in {"length", "max_tokens", "maxtokens"}:
        raise ValueError(
            f"LLM response truncated at {max_tokens}-token limit for {model} — raise cap or lower reasoning"
        )
    return choice.message.content


def parse_andy_response(text: str) -> dict:
    """Extract a JSON object from an LLM response.

    Tolerates markdown code fences and surrounding prose/reasoning by falling back
    to the outermost {...}. Raises ValueError (with a snippet of what came back) if
    no JSON object can be parsed — so the failure is diagnosable, not a bare decode error.
    """
    if not text or not text.strip():
        raise ValueError("LLM returned an empty response")

    cleaned = text.strip()
    if cleaned.startswith("```"):
        # drop the opening ```lang line and a trailing ``` fence, wherever it lands
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
        cleaned = cleaned.strip()

    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        try:
            result = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result

    # head + tail + length so the failure is self-diagnosing: ending in '}' means
    # complete-but-unparseable; ending mid-string means truncated.
    raise ValueError(f"LLM did not return a JSON object (len={len(text)}): {text[:120]!r}…{text[-80:]!r}")
