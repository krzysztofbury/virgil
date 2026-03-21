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


async def call_llm(db, system_prompt: str, user_prompt: str) -> str:
    """Call an LLM using the resolved provider (user or internal fallback).

    Returns the assistant's text response.
    """
    model, api_key = await _resolve_provider(db)

    try:
        response = await litellm.acompletion(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=api_key,
            max_tokens=1024,
            timeout=60.0,
        )
    except litellm.AuthenticationError:
        raise ValueError(f"LLM authentication failed for model {model} — check your API key") from None
    except litellm.RateLimitError:
        raise ValueError(f"LLM rate limit exceeded for model {model} — try again later") from None
    except litellm.Timeout:
        raise ValueError(f"LLM request timed out for model {model}") from None
    except litellm.APIError as exc:
        raise ValueError(f"LLM API error for model {model}: {exc}") from exc

    return response.choices[0].message.content


def parse_andy_response(text: str) -> dict:
    """Extract JSON from LLM response, handling markdown code fences."""
    assert text, "LLM response is empty"
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]  # skip ```json
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    result = json.loads(cleaned)
    assert isinstance(result, dict), f"Expected dict from LLM, got {type(result).__name__}"
    return result
