import json
import logging

import litellm

from app.services.encryption import decrypt

logger = logging.getLogger(__name__)

# Suppress litellm's verbose default logging.
litellm.suppress_debug_info = True


class LLMCallAmbiguousError(RuntimeError):
    """The provider may have accepted a paid request before transport failed."""


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
        raise LLMCallAmbiguousError(f"LLM request timed out for model {model}") from None
    except litellm.APIError as exc:
        raise LLMCallAmbiguousError(f"LLM API outcome is uncertain for model {model}: {exc}") from exc

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


def _repair_truncated_json(text: str, start: int) -> dict | None:
    """Salvage a truncated JSON object by discarding the incomplete tail.

    Truncated output - thinking eating into the output allowance - is the
    dominant real-world failure, and by the time it happens most of the useful
    payload has already arrived. This scans for every structural boundary where
    an element ends (a closed container, or a comma), then works from the LAST
    boundary backwards: cut the tail off there and close whatever brackets are
    still open. Longest repairable prefix wins, so nothing salvageable is lost.

    This replaces appending a single "}" or '"}', which closes exactly ONE
    level. That was enough for the flat 4-field A.N.D.Y. object it was written
    for, but the WOD parser's payload nests three deep -
    {"entries": [{...}, {...}]} - so no single suffix could ever repair it. The
    repair was silently dead code for that caller: a note whose first 25
    movements had parsed perfectly was discarded whole.

    Returns the first candidate that decodes to a NON-EMPTY dict, or None. An
    empty dict is refused deliberately: '{' alone repairs to '{}', which is
    indistinguishable from "the model said nothing useful" and must surface as
    a failure rather than as a successful parse of nothing.
    """
    # raw_decode stops at the first syntax error, so an attempt costs the walk up
    # to that error - not the whole response. Cheap for a genuine truncation,
    # where the error is at the very end and the first attempt succeeds. The
    # expensive shape is a body that is malformed in the MIDDLE and carries
    # thousands more boundaries after it: every candidate cut past the halfway
    # point pays the full walk to it, and the total goes quadratic. Measured at
    # 1.9s on a 32 KB body, against a max_tokens that permits roughly twice that.
    #
    # Capping attempts is not a lost repair. Candidates are tried from the
    # truncation point backwards, so a real cut is salvaged within an attempt or
    # two; what the unbounded scan eventually finds on a mid-document error is a
    # cut that silently discards half the response to route around it, which is
    # worse than the diagnostic ValueError the caller already handles.
    MAX_REPAIR_ATTEMPTS = 64

    def closers_for(open_brackets: list[str]) -> str:
        return "".join("]" if c == "[" else "}" for c in reversed(open_brackets))

    stack: list[str] = []
    in_string = False
    escape = False
    # (cut, closers): text[:cut] + closers is a structurally complete document.
    candidates: list[tuple[int, str]] = []

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in "{[":
            stack.append(ch)
            continue
        if ch in "}]":
            if not stack:
                # The outermost object closed. raw_decode already handles a
                # complete object plus trailing junk, so there is nothing
                # further out to salvage.
                break
            stack.pop()
        elif ch != ",":
            continue
        # A container just closed, or an element just ended at a comma. Cutting
        # AT the comma (not past it) is what drops the trailing comma that would
        # otherwise make the repaired document invalid.
        if stack:
            candidates.append((i if ch == "," else i + 1, closers_for(stack)))

    # Closing at the very end is the only candidate for a shallow cut
    # ('{"a": 1') - there is no earlier boundary to fall back to - and for a
    # deep cut it keeps the final half-written element's fields instead of
    # discarding them. An unterminated string gets its closing quote back too,
    # unless the cut landed on a backslash, where the escape would swallow it.
    if stack:
        tail = text.rstrip()
        if in_string and not escape:
            candidates.append((len(tail), '"' + closers_for(stack)))
        elif not in_string:
            candidates.append((len(tail.rstrip(",")), closers_for(stack)))

    for cut, closers in reversed(candidates[-MAX_REPAIR_ATTEMPTS:]):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[:cut] + closers, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj:
            logger.warning(
                "Repaired truncated LLM JSON (len=%d, kept %d chars, closed %r)", len(text), cut - start, closers
            )
            return obj
    return None


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

        repaired = _repair_truncated_json(text, start)
        if repaired is not None:
            return repaired

    # head + tail + length so the failure is self-diagnosing: ending in '}' means
    # complete-but-unparseable; ending mid-string means truncated.
    raise ValueError(f"LLM did not return a JSON object (len={len(text)}): {text[:120]!r}…{text[-80:]!r}")
