# LiteLLM Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hand-rolled LLM HTTP clients with LiteLLM, add internal provider fallback via env vars, expand Settings UI to support more providers.

**Architecture:** Single `litellm.acompletion()` call replaces three provider-specific HTTP clients. Two-tier resolution: user-configured DB provider first, env-var internal provider as fallback. DB migration converts old provider/model strings to LiteLLM format.

**Tech Stack:** LiteLLM, FastAPI, aiosqlite, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-21-litellm-migration-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `pyproject.toml` | Modify | Add `litellm` dependency |
| `app/config.py` | Modify | Add `INTERNAL_LLM_MODEL`, `INTERNAL_LLM_KEY` |
| `app/services/llm.py` | Rewrite | Replace httpx clients with litellm.acompletion, add fallback chain |
| `app/migrations/007_litellm_model_strings.py` | Create | Convert existing provider data to LiteLLM format |
| `app/routers/settings.py` | Modify | Update `add_llm_provider` to store LiteLLM model strings |
| `app/templates/settings.html` | Modify | Expand provider dropdown, add model auto-fill JS |
| `.env.example` | Modify | Add internal LLM env vars |

---

### Task 1: Add LiteLLM dependency

**Files:**
- Modify: `pyproject.toml:6-19` (dependencies list)

- [ ] **Step 1: Add litellm to dependencies**

In `pyproject.toml`, add `litellm` to the dependencies list:

```toml
dependencies = [
    "fastapi==0.115.6",
    "uvicorn[standard]==0.34.0",
    "jinja2==3.1.5",
    "python-multipart==0.0.20",
    "aiosqlite==0.20.0",
    "httptools==0.6.4",
    "httpx>=0.28.1",
    "cryptography>=46.0.5",
    "bcrypt>=5.0.0",
    "pyotp>=2.9.0",
    "itsdangerous>=2.2.0",
    "qrcode[pil]>=8.2",
    "litellm>=1.60.0",
]
```

- [ ] **Step 2: Install**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv sync`
Expected: Resolves and installs litellm and its dependencies.

- [ ] **Step 3: Verify import works**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python -c "import litellm; print(litellm.__version__)"`
Expected: Prints a version number like `1.xx.x`.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add litellm for unified LLM provider access"
```

---

### Task 2: Add internal LLM config

**Files:**
- Modify: `app/config.py` (add 2 env vars)
- Modify: `.env.example` (document new vars)

- [ ] **Step 1: Add env vars to config.py**

Add at the end of `/Users/krzysztofbury/PRIV/virgil/app/config.py`:

```python
# Internal LLM — used for onboarding and system features.
# Fallback when no user-configured provider is active.
INTERNAL_LLM_MODEL = os.environ.get("VIRGIL_INTERNAL_LLM_MODEL", "gemini/gemini-3-flash-preview")
INTERNAL_LLM_KEY = os.environ.get("VIRGIL_INTERNAL_LLM_KEY", "")
```

- [ ] **Step 2: Update .env.example**

Replace the old LLM section in `/Users/krzysztofbury/PRIV/virgil/.env.example`:

```bash
# ── Virgil — Environment Variables ────────────────────────────

# Cloudflare Tunnel token — from Zero Trust > Networks > Tunnels
CLOUDFLARE_TUNNEL_TOKEN=

# Encryption key for LLM API keys at rest (optional)
# If empty, auto-generates to /data/virgil.key on first run
VIRGIL_ENCRYPTION_KEY=

# Internal LLM — used for onboarding and system features
# Default model: gemini/gemini-3-flash-preview (requires Google AI API key)
# Override with any LiteLLM model string (e.g., anthropic/claude-sonnet-4-20250514)
VIRGIL_INTERNAL_LLM_MODEL=gemini/gemini-3-flash-preview
VIRGIL_INTERNAL_LLM_KEY=
```

- [ ] **Step 3: Commit**

```bash
git add app/config.py .env.example
git commit -m "config: add internal LLM model and key env vars"
```

---

### Task 3: Rewrite LLM service to use LiteLLM

**Files:**
- Rewrite: `app/services/llm.py`

- [ ] **Step 1: Rewrite llm.py**

Replace the entire contents of `/Users/krzysztofbury/PRIV/virgil/app/services/llm.py` with:

```python
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

    raise ValueError(
        "No LLM provider available — configure one in Settings or set VIRGIL_INTERNAL_LLM_KEY"
    )


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
        raise ValueError(f"LLM authentication failed for model {model} — check your API key")
    except litellm.RateLimitError:
        raise ValueError(f"LLM rate limit exceeded for model {model} — try again later")
    except litellm.Timeout:
        raise ValueError(f"LLM request timed out for model {model}")
    except litellm.APIError as exc:
        raise ValueError(f"LLM API error for model {model}: {exc}")

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
```

- [ ] **Step 2: Run lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/services/llm.py && uv run ruff format app/services/llm.py`
Expected: No errors.

- [ ] **Step 3: Smoke test — verify import and function signatures**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python -c "from app.services.llm import call_llm, get_active_provider, parse_andy_response; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add app/services/llm.py
git commit -m "feat: replace hand-rolled LLM clients with LiteLLM acompletion"
```

---

### Task 4: Create migration 007 — convert existing provider data

**Files:**
- Create: `app/migrations/007_litellm_model_strings.py`

- [ ] **Step 1: Create migration file**

Create `/Users/krzysztofbury/PRIV/virgil/app/migrations/007_litellm_model_strings.py`:

```python
"""Convert existing llm_providers rows to LiteLLM model string format.

Old format: provider='claude', model='claude-sonnet-4-20250514'
New format: provider='anthropic', model='anthropic/claude-sonnet-4-20250514'
"""

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
            # Only prefix if not already in LiteLLM format.
            new_model = f"{prefix}{old_model}" if not old_model.startswith(prefix) else old_model
            await db.execute(
                "UPDATE llm_providers SET provider = ?, model = ? WHERE id = ?",
                (new_provider, new_model, row["id"]),
            )
```

- [ ] **Step 2: Run lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/migrations/007_litellm_model_strings.py`
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add app/migrations/007_litellm_model_strings.py
git commit -m "migration: convert llm_providers to LiteLLM model string format"
```

---

### Task 5: Update Settings UI — expanded provider dropdown with model auto-fill

**Files:**
- Modify: `app/templates/settings.html:79-103` (LLM add-provider form)
- Modify: `app/routers/settings.py:282-295` (add_llm_provider handler)

- [ ] **Step 1: Update the settings template**

Replace lines 79-103 in `/Users/krzysztofbury/PRIV/virgil/app/templates/settings.html` (the `<details>` block for "Add provider") with:

```html
    <details>
        <summary style="cursor:pointer;font-weight:500;margin-bottom:0.75rem;">Add provider</summary>
        <form method="POST" action="/settings/llm/add" x-data="{
            provider: 'anthropic',
            models: {
                'anthropic': 'anthropic/claude-sonnet-4-20250514',
                'openai': 'openai/gpt-4o-mini',
                'gemini': 'gemini/gemini-3-flash-preview',
                'mistral': 'mistral/mistral-small-latest',
                'groq': 'groq/llama-3.3-70b-versatile',
                'ollama': 'ollama/llama3.2',
                'other': ''
            },
            model: 'anthropic/claude-sonnet-4-20250514',
            isOther: false,
            setProvider(val) {
                this.provider = val;
                this.isOther = val === 'other';
                if (!this.isOther) this.model = this.models[val];
            }
        }">
            <div class="grid" style="margin-bottom:0.5rem;">
                <div>
                    <label>Provider
                        <select name="provider" required x-model="provider" @change="setProvider($event.target.value)">
                            <option value="anthropic">Anthropic</option>
                            <option value="openai">OpenAI</option>
                            <option value="gemini">Google Gemini</option>
                            <option value="mistral">Mistral</option>
                            <option value="groq">Groq</option>
                            <option value="ollama">Ollama (local)</option>
                            <option value="other">Other (LiteLLM)</option>
                        </select>
                    </label>
                </div>
                <div>
                    <label>Model
                        <input type="text" name="model" x-model="model" required
                               :placeholder="isOther ? 'e.g. together_ai/meta-llama/Llama-3-70b' : ''">
                    </label>
                </div>
            </div>
            <label>API Key
                <input type="password" name="api_key" placeholder="sk-..." required>
            </label>
            <button type="submit" class="btn btn-primary" style="margin-top:0.5rem;">Add</button>
        </form>
    </details>
```

- [ ] **Step 2: Update the add_llm_provider route**

In `/Users/krzysztofbury/PRIV/virgil/app/routers/settings.py`, replace lines 282-295 with:

```python
@router.post("/settings/llm/add")
async def add_llm_provider(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(...),
):
    from app.validation import truncate

    db = await get_db()
    # Sanitize inputs — provider and model are stored as-is for LiteLLM.
    provider = truncate(provider.strip(), 50)
    model = truncate(model.strip(), 200)
    if not provider or not model or not api_key:
        return RedirectResponse("/settings?tab=general&err=All+fields+required", status_code=303)
    await db.execute(
        "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 0)",
        (provider, encrypt(api_key), model),
    )
    await db.commit()
    return RedirectResponse("/settings?tab=general", status_code=303)
```

- [ ] **Step 3: Run lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/settings.py && uv run ruff format app/routers/settings.py`
Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add app/templates/settings.html app/routers/settings.py
git commit -m "feat: expand LLM provider dropdown with model auto-fill and LiteLLM support"
```

---

### Task 6: End-to-end verification

- [ ] **Step 1: Kill any running Virgil server**

Run: `lsof -ti:8123 | xargs kill -9 2>/dev/null; true`

- [ ] **Step 2: Start the server**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python -m app`
Expected: Server starts on port 8123 with no import errors.

- [ ] **Step 3: Verify migration ran**

Check logs for: `Applying migration 007: 007_litellm_model_strings.py`
If user had existing providers, verify they were converted.

- [ ] **Step 4: Verify Settings UI**

Open http://localhost:8123/settings?tab=general:
- Provider dropdown shows 7 options (Anthropic, OpenAI, Google Gemini, Mistral, Groq, Ollama, Other)
- Changing provider auto-fills model field
- "Other" clears model field for free-text input

- [ ] **Step 5: Test adding a provider**

Add a test provider via the UI and verify it appears in the table.

- [ ] **Step 6: Test internal fallback**

Set `VIRGIL_INTERNAL_LLM_KEY` in `.env` and verify that briefing/ANDY generation works even with no user-configured provider active.

- [ ] **Step 7: Final commit**

```bash
git add -A
git commit -m "feat: complete LiteLLM migration — unified LLM with internal fallback"
```
