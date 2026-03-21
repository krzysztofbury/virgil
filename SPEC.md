# SPEC.md - Technical Specification for Virgil

This document describes the architecture, conventions, and design decisions for AI agents and contributors working with this codebase.

## Project Overview

**Virgil** is a single-user, self-hosted life-tracking dashboard built with FastAPI, SQLite, Jinja2, and HTMX.

- **Language:** Python 3.12+
- **Package manager:** [uv](https://github.com/astral-sh/uv)
- **Formatter/Linter:** [ruff](https://github.com/astral-sh/ruff) (line-length: 120, target: py312)
- **Security scanner:** [bandit](https://github.com/PyCQA/bandit)
- **Database:** SQLite with WAL mode, versioned migration system
- **Frontend:** Jinja2 templates + HTMX + Alpine.js + Chart.js
- **License:** MIT

## Architecture

```
app/
  __main__.py           - Entry point: uvicorn launcher
  main.py               - FastAPI app, Jinja2 setup, middleware stack, routes
  config.py             - Environment variable configuration
  db.py                 - Database connection, schema constants, seed data
  auth.py               - Authentication middleware (cookie sessions)
  csrf.py               - CSRF double-submit cookie middleware
  rate_limit.py         - Per-IP sliding window rate limiter
  security_headers.py   - Security response headers (CSP, etc.)
  validation.py         - Shared input validation helpers

  migrations/           - Numbered database schema migrations
    runner.py           - Migration discovery + execution engine
    001_*.py ... 006_*.py

  models/               - Data models (query helpers, not ORMs)
    daily.py, bloodwork.py, experiments.py, feniks.py,
    goals.py, life_scores.py, oura.py

  routers/              - FastAPI route handlers (one per feature)
    auth.py, dashboard.py, daily.py, training.py, feniks.py,
    oura.py, oura_webhook.py, bloodwork.py, life_scores.py,
    goals.py, experiments.py, settings.py

  services/             - Business logic (no HTTP concerns)
    encryption.py       - Fernet encrypt/decrypt for secrets
    oura_api.py         - Oura OAuth2 + API v2 client + sync
    llm.py              - LLM API client (Claude/OpenAI/Gemini)
    briefing.py         - AI morning briefing generation
    markdown_export.py  - Scoped markdown export
    markdown_import.py  - Markdown to DB parsers (onboarding)
    scheduler.py        - Background task loop (backup + sync + export)
    backup.py           - SQLite backup with rolling retention
    experiment_summary.py - AI weekly summaries
    streak.py           - Feniks streak calculation

  templates/            - Jinja2 HTML templates (19 files)
    base.html           - Layout, nav, theme toggle, SW registration
    partials/           - HTMX partial response templates

  static/               - Frontend assets
    css/app.css         - Design system, dark/light theme via CSS vars
    js/app.js           - Theme, keyboard shortcuts, swipe, toggles, toasts
    js/charts.js        - Theme-aware Chart.js wrappers
    service-worker.js   - PWA service worker
    manifest.json       - PWA manifest
```

### Key Design Decisions

1. **SQLite is the single source of truth.** No ORM — raw SQL via `aiosqlite`. Models are query helpers, not mapped objects.
2. **HTMX for interactivity.** No SPA framework. Server renders HTML, HTMX swaps partials. Alpine.js for local state (toggles, counters).
3. **Numbered migrations instead of `CREATE TABLE IF NOT EXISTS`.** Each migration is a Python file with an `async def up(db)` function. Applied sequentially on startup, tracked in `schema_migrations`.
4. **Fernet encryption for secrets at rest.** OAuth tokens, LLM API keys, and webhook secrets are encrypted in the database. Key is auto-generated or provided via env var.
5. **Single-user model.** No multi-tenancy. One authenticated user per instance.
6. **Background scheduler.** An asyncio loop handles periodic tasks (backup, Oura sync, markdown export) without external dependencies like Celery.

## Middleware Stack (Processing Order)

1. **Security Headers** — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
2. **Rate Limiting** — 120 req/min general, 10 req/min for auth endpoints (per-IP sliding window)
3. **Authentication** — Cookie-based session verification (exempts: login, setup, MFA, offline, service worker, Oura webhook)
4. **Feature Flags** — Loads `feature_*` settings into `request.state.features`
5. **CSRF Protection** — Double-submit cookie on all POST forms (exempts: Oura webhook)

## How to Work With This Codebase

### Setup

```bash
uv sync
cp .env.example .env  # Fill in optional API keys
```

### Running

```bash
# Development (hot reload)
uv run python -m app

# Production
VIRGIL_ENV=prod uv run python -m app
```

### Linting and Formatting

```bash
ruff check app/ scripts/ --fix
ruff format app/ scripts/
bandit -c pyproject.toml -r app/
```

### Adding a New Feature Page

1. Create a model in `app/models/` with query helpers
2. Create a router in `app/routers/` with route handlers
3. Create a template in `app/templates/`
4. Register the router in `app/main.py`
5. Add navigation link in `app/templates/base.html`
6. Add keyboard shortcut in `app/static/js/app.js`

### Adding a Database Migration

1. Create `app/migrations/NNN_name.py` with next sequence number
2. Expose `async def up(db)` — receives an `aiosqlite.Connection`
3. Migration must be idempotent (use `IF NOT EXISTS`, `IF EXISTS` guards)
4. Runner commits each migration individually

### Adding an Integration

1. Add OAuth2/API client in `app/services/`
2. Store credentials encrypted via `app/services/encryption.py`
3. Add webhook endpoint in `app/routers/` (exempt from CSRF)
4. Verify webhook signatures with HMAC-SHA256
5. Add connection UI in Settings > Integrations tab

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `VIRGIL_ENV` | `local` | `local` (hot reload) or `prod` (no reload) |
| `VIRGIL_DB_PATH` | `./data/virgil.db` | SQLite database path |
| `VIRGIL_SECOND_BRAIN_PATH` | (empty) | Path to markdown export directory |
| `VIRGIL_HOST` | `0.0.0.0` | Server bind host |
| `VIRGIL_BASE_URL` | `http://localhost:8123` | Public URL for OAuth callbacks |
| `VIRGIL_ENCRYPTION_KEY` | (auto-generated) | Fernet key for secret encryption |

Port is always **8123**.

## Important Constraints

- **Single-user only.** All routes assume one authenticated user.
- **No JavaScript build step.** All JS is vanilla, loaded from CDN or `/static/`.
- **No ORM.** Raw SQL only. This is intentional — keeps queries explicit and auditable.
- **SQLite limitations.** No concurrent writes from multiple processes. WAL mode allows concurrent reads.
- **Service worker caching.** Static assets use cache-first. If you change CSS/JS, users may need to hard-refresh or wait for the SW to update.
