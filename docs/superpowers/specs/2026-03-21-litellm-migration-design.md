# LiteLLM Migration Design

## Problem

Virgil's `app/services/llm.py` contains hand-rolled HTTP clients for three providers (Claude, OpenAI, Gemini). Each provider has its own `_call_*` function with bespoke request/response handling. Adding a new provider means writing another HTTP client. There is also no "internal" LLM — all features require the user to have configured an API key first, which blocks onboarding and system-level features.

## Solution

Replace the custom HTTP code with [LiteLLM](https://github.com/BerriAI/litellm), which provides a unified `acompletion()` interface across 100+ LLM providers. Introduce a two-tier LLM system:

- **Internal tier** — configured via environment variables, always available for system features (onboarding, future autonomous tasks). Default: `gemini/gemini-3-flash-preview`.
- **User tier** — configured in Settings UI, optional. Supports any LiteLLM-compatible provider.

**Fallback chain:** user-configured provider first, internal provider as fallback.

## Architecture

### Provider Resolution

```
call_llm(db, system_prompt, user_prompt)
  │
  ├─ 1. Check DB for active user-configured provider
  │     → if found, use user's model + decrypted key
  │
  └─ 2. Fall back to internal provider (env vars)
        → VIRGIL_INTERNAL_LLM_MODEL + VIRGIL_INTERNAL_LLM_KEY
        → if neither configured, raise ValueError
```

### Files Changed

#### `app/services/llm.py` (rewrite)

Current: 104 lines with `_call_claude`, `_call_openai`, `_call_gemini` functions.

New:

```python
import litellm

async def call_llm(db, system_prompt: str, user_prompt: str) -> str:
    """Call LLM using user provider (preferred) or internal fallback."""
    model, api_key = await _resolve_provider(db)

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
    return response.choices[0].message.content


async def _resolve_provider(db) -> tuple[str, str]:
    """Return (model_string, api_key). User provider first, internal fallback."""
    # Try user-configured provider
    provider = await get_active_provider(db)
    if provider:
        api_key = decrypt(provider["api_key_enc"])
        model = provider["model"]  # Already a LiteLLM model string
        if api_key and model:
            return model, api_key

    # Fall back to internal
    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL
    if INTERNAL_LLM_KEY and INTERNAL_LLM_MODEL:
        return INTERNAL_LLM_MODEL, INTERNAL_LLM_KEY

    raise ValueError("No LLM provider available — configure one in Settings or set VIRGIL_INTERNAL_LLM_KEY")
```

Functions retained unchanged:
- `get_active_provider(db)` — reads from `llm_providers` table
- `parse_andy_response(text)` — extracts JSON from LLM response

Functions removed:
- `_call_claude`, `_call_openai`, `_call_gemini` — replaced by `litellm.acompletion`

#### `app/config.py`

Add two new env vars:

```python
INTERNAL_LLM_MODEL = os.environ.get("VIRGIL_INTERNAL_LLM_MODEL", "gemini/gemini-3-flash-preview")
INTERNAL_LLM_KEY = os.environ.get("VIRGIL_INTERNAL_LLM_KEY", "")
```

#### `app/routers/settings.py` (Settings UI changes)

Curated provider dropdown with default models:

| Provider | LiteLLM prefix | Default model |
|----------|---------------|---------------|
| Anthropic | `anthropic/` | `anthropic/claude-sonnet-4-20250514` |
| OpenAI | `openai/` | `openai/gpt-4o-mini` |
| Google Gemini | `gemini/` | `gemini/gemini-3-flash-preview` |
| Mistral | `mistral/` | `mistral/mistral-small-latest` |
| Groq | `groq/` | `groq/llama-3.3-70b-versatile` |
| Ollama (local) | `ollama/` | `ollama/llama3.2` |
| Other (LiteLLM) | (user types) | (user types full model string) |

When user selects a provider, the model field auto-populates with the default. User can override. The full LiteLLM model string (e.g., `anthropic/claude-sonnet-4-20250514`) is stored in the `model` column of `llm_providers`.

For "Other (LiteLLM)", the model field becomes free-text and the user enters the complete LiteLLM model string.

#### `app/templates/settings.html`

Update the LLM provider form:
- Replace the 3-option provider select with the expanded dropdown
- Add JS to auto-fill model default on provider change
- Show "Model string" input for "Other" option

#### `.env.example`

Add:

```bash
# Internal LLM (used for onboarding and system features)
# Default: gemini/gemini-3-flash-preview — requires a Google AI API key
VIRGIL_INTERNAL_LLM_MODEL=gemini/gemini-3-flash-preview
VIRGIL_INTERNAL_LLM_KEY=
```

#### `pyproject.toml`

Add dependency:

```toml
"litellm>=1.60.0",
```

### Database

**No schema changes.** The `llm_providers` table already has:
- `provider` TEXT — will store the LiteLLM prefix (e.g., `anthropic`)
- `model` TEXT — will store the full LiteLLM model string (e.g., `anthropic/claude-sonnet-4-20250514`)
- `api_key_enc` TEXT — Fernet-encrypted API key
- `is_active` INTEGER — 0 or 1

Existing rows with old provider/model values (e.g., `provider=claude`, `model=claude-sonnet-4-20250514`) need a one-time migration to LiteLLM format (`provider=anthropic`, `model=anthropic/claude-sonnet-4-20250514`). This will be a new migration `007_litellm_model_strings.py`.

### Migration 007: Convert existing provider data

```python
PROVIDER_MAP = {
    "claude": ("anthropic", "anthropic/"),
    "openai": ("openai", "openai/"),
    "gemini": ("gemini", "gemini/"),
}

async def up(db):
    rows = await db.execute_fetchall("SELECT id, provider, model FROM llm_providers")
    for row in rows:
        old_provider = row["provider"]
        old_model = row["model"]
        if old_provider in PROVIDER_MAP:
            new_provider, prefix = PROVIDER_MAP[old_provider]
            new_model = f"{prefix}{old_model}" if not old_model.startswith(prefix) else old_model
            await db.execute(
                "UPDATE llm_providers SET provider = ?, model = ? WHERE id = ?",
                (new_provider, new_model, row["id"]),
            )
```

### What Doesn't Change

- `app/services/briefing.py` — calls `call_llm(db, ...)`, interface identical
- `app/services/experiment_summary.py` — calls `call_llm(db, ...)`, interface identical
- `app/routers/daily.py` — calls `call_llm(db, ...)`, interface identical
- `app/services/encryption.py` — still encrypts/decrypts API keys
- `llm_providers` table schema — no structural changes

### Error Handling

LiteLLM raises specific exceptions:
- `litellm.AuthenticationError` — bad API key
- `litellm.RateLimitError` — 429 from provider
- `litellm.Timeout` — request timed out
- `litellm.APIError` — generic provider error

`call_llm` will catch these and re-raise as `ValueError` with human-readable messages to maintain the current error contract with callers.

### Testing

- Verify each curated provider works with a real key
- Verify "Other" free-form model string works
- Verify fallback chain: user key → internal key → error
- Verify migration 007 converts existing provider data correctly
- Verify Settings UI auto-populates model defaults
