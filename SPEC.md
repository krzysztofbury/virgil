# SPEC.md - Technical Specification for Virgil

This document describes the architecture, conventions, and design decisions for AI agents and contributors working with this codebase.

## Project Overview

**Virgil** is a multi-user, self-hosted life-tracking dashboard built with FastAPI, SQLite, Jinja2, and HTMX. Identities live in a central registry database; each user's data lives in an isolated per-user SQLite database.

- **Language:** Python 3.14
- **Package manager:** [uv](https://github.com/astral-sh/uv)
- **Formatter/Linter:** [ruff](https://github.com/astral-sh/ruff) (line-length: 120, target: py314)
- **Security scanner:** [bandit](https://github.com/PyCQA/bandit)
- **Database:** SQLite with WAL mode, versioned migration system
- **Frontend:** Jinja2 templates + HTMX + Alpine.js + Chart.js
- **License:** MIT

## Architecture

```
app/
  __main__.py           - Entry point: uvicorn launcher
  main.py               - FastAPI app, Jinja2 setup, middleware stack, routers
  config.py             - Environment variable configuration
  server_settings.py    - Settings an admin changes in Settings > Server; the environment is the fallback
  user_db.py            - Opens, creates and deletes each account's own SQLite database
  central_db.py         - The central registry: users, sessions, tokens, invites, webhook routes, subscriptions
  central_schema.py     - The frozen schema that central migration 001 creates
  db.py                 - Life-area constants, app_settings helpers and the per-account feature flags
  auth.py, csrf.py, rate_limit.py, security_headers.py - Middleware
  validation.py         - Shared input validation helpers
  mcp_endpoint.py       - Hosted MCP at /mcp (the tools of mcp_server/virgil_mcp.py)

  migrations/           - Numbered per-account schema migrations (runner.py + NNN_name.py)
  central_migrations/   - Numbered central registry migrations, tracked separately
  models/               - Query helpers that predate the services layer
  routers/              - Request handlers, one module per page or surface (api.py is the REST API)
  services/             - Business logic, no HTTP concerns; one module per feature
                          (finance.py, freedom_log.py, streak.py, briefing.py, jobs.py, ...)
  templates/            - Jinja2 pages; partials/ holds HTMX partials and shared macros
  static/               - css/app.css, js/app.js, js/charts.js, the service worker and the PWA manifest
  locale/               - The Polish catalog (scripts/i18n.py keeps it in step)

mcp_server/virgil_mcp.py - MCP stdio server with its own lock file; calls the REST API
scripts/                - Release (deploy.py), recovery, demo seed, i18n, screenshots and checks
infra/                  - The hosted instance: Ansible host setup and Compose/env templates
docs/                   - Runbooks: recovery, load check, post-deploy evidence
tests/                  - pytest suite; snapshots/ holds the schema snapshots
```

### Key Design Decisions

1. **SQLite is the single source of truth.** No ORM — raw SQL via `aiosqlite`. Models are query helpers, not mapped objects.
2. **HTMX for interactivity.** No SPA framework. Server renders HTML, HTMX swaps partials. Alpine.js for local state (toggles, counters).
3. **Numbered migrations instead of `CREATE TABLE IF NOT EXISTS`.** Each migration is a Python file with an `async def up(db)` function. Applied sequentially on startup, tracked in `schema_migrations`.
4. **Fernet encryption for secrets at rest.** OAuth tokens, LLM API keys, and webhook secrets are encrypted in the database. Key is auto-generated or provided via env var.
5. **Multi-user model.** Central `virgil-central.db` user registry; one isolated SQLite database per user (`data/users/{uuid}.db`), opened per request by the auth middleware.
6. **Training data model.** The WOD confirm screen (`POST /training/wod/confirm`) is the only writer of `training_entries`. `training_entries.duration` is SECONDS (migration 020) and renders through `app.formatting.format_duration_seconds`. `training_entries.notes` holds the metcon result for that movement: a finishing time, or rounds plus reps. A `training_sessions` row with a non-NULL `wod_parsed` has a parse waiting for review, and `/training` lists every one of them. History is grouped into a year-addressable calendar backed by one aggregate row per date; selecting a day loads bounded pages of its sessions and sets. Personal Bests group maximum loads by movement and comparable effort (rep count or timed duration). `training_sessions.capture_token` (migration 024) makes one rendered capture form write one session. `training_exercises.name` is unique with SQLite `NOCASE` semantics (migration 026), and movement resolution uses an upsert so concurrent WOD confirmations return one row. `exercise_library.sets` / `reps` are parser fallbacks, not a prescription; `training_exercises.ad_hoc` is provenance with no reader.
7. **Page shape.** Five pages converged on one shape in v0.6.0, and a sixth should not invent another. The first block is what to do now: the current state stated in words or numbers, plus one primary action. Reference and analytics follow, under an `Insights` or `Your trends` heading. History is last. A secondary form and every destructive action live behind a `<details>`; an input inside a closed `<details>` still submits, so nothing is lost by collapsing one. A `<details>` never contains a Chart.js canvas: a canvas with no layout renders at zero size and stays wrong after opening, so charts get reordered instead. A mobile-only view pairs `.on-mobile` with `.on-desktop` and never duplicates markup - one Jinja macro renders both. Layout lives in `app/static/css/app.css`; a page-level inline `grid-template-columns: repeat(<n>, ...)` outranks every media query and `tests/test_mobile_layout.py` refuses it.
8. **Background scheduler.** An asyncio loop handles periodic tasks (backup, Oura sync, briefings, experiment summaries) without external dependencies like Celery.

## Middleware Stack (Processing Order)

1. **Security Headers** - CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, on every response the layers below produce (errors and redirects included)
2. **Central migration guard** - 503 for everything but `/healthz` while the registry failed to migrate
3. **Rate Limiting** - 120 req/min general, 10 req/min for auth endpoints (per-IP sliding window). `CF-Connecting-IP` is accepted only when Cloudflare trust is enabled and the socket peer is in `VIRGIL_TRUSTED_PROXY_IPS` (addresses or CIDR ranges).
4. **Authentication** - Cookie-based session verification against the account's session epoch (exempts: login, signup, MFA, offline, healthz, service worker, per-user Oura webhooks). Also loads `feature_*` settings into `request.state.features`
5. **CSRF Protection** - Double-submit cookie on all POST forms (exempts: Oura webhook, API-key routes). The only layer that reads a request body, so it runs last; a write without the CSRF cookie is refused before its body is read

## How to Work With This Codebase

### Setup

```bash
uv sync --locked --extra dev --python 3.14.9
cp .env.example .env  # Fill in optional API keys
```

### Running

```bash
# Development (hot reload)
uv run --locked python -m app

# Production
VIRGIL_ENV=prod uv run --locked python -m app
```

### Linting and Formatting

```bash
uv run --locked --extra dev ruff check app/ scripts/ tests/ mcp_server/
uv run --locked --extra dev ruff format --check app/ scripts/ tests/ mcp_server/
uv run --locked --extra dev bandit -q -c pyproject.toml -r app/ scripts/ mcp_server/
```

### Adding a New Feature Page

1. Put persistence in a service in `app/services/`, which owns validation,
   transaction boundaries and rollback
2. Create a router in `app/routers/` with route handlers
3. Create a template in `app/templates/`
4. Register the router in `app/main.py`
5. Add navigation link in `app/templates/base.html`: the top bar, and on phones
   either the bottom bar (a daily action) or its `More` menu (read-mostly pages)
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
| `VIRGIL_CENTRAL_DB_PATH` | `./data/virgil-central.db` | Central user registry (per-user DBs live in `data/users/`) |
| `VIRGIL_HOST` | `0.0.0.0` | Server bind host |
| `VIRGIL_BASE_URL` | `http://localhost:8123` | Public URL for OAuth callbacks |
| `VIRGIL_ENCRYPTION_KEY` | (auto-generated) | Fernet key for secret encryption |
| `VIRGIL_TRUST_CLOUDFLARE_HEADERS` | `false` | Allow trusted proxy peers to provide `CF-Connecting-IP` |
| `VIRGIL_TRUSTED_PROXY_IPS` | (empty) | Comma-separated peer IPs or CIDR ranges trusted as proxies |

Port is always **8123**.

## Important Constraints

- **Multi-user.** Routes operate on the per-user database from `request.state.user_db` (`get_user_db_from_request`); never use the legacy global `get_db()`.
- **No JavaScript build step.** All JS is vanilla, loaded from CDN or `/static/`.
- **No ORM.** Raw SQL only. This is intentional — keeps queries explicit and auditable.
- **SQLite limitations.** No concurrent writes from multiple processes. WAL mode allows concurrent reads.
- **Service worker caching.** Static assets use cache-first. If you change CSS/JS, users may need to hard-refresh or wait for the SW to update.

## Verification and recovery contracts

CI is defined once in `.github/workflows/ci.yml`. PRs verify and smoke-test an
image; main publishes that same tested image. A version tag aliases an existing
main image and cannot update `latest`. Python, uv, application dependencies, MCP
dependencies, and base-image digests are explicit. See [RELEASE.md](RELEASE.md).

Exercise-library persistence belongs to `app/services/library.py`, with one
transaction for validation, collision checks, row changes, and tags. Browser and
REST handlers translate the same domain outcomes into their respective protocols.
Markdown imports require a registered account; runtime code has no global legacy
DB connection. Whole-instance recovery includes the key and central registry as
well as per-user databases; [docs/recovery.md](docs/recovery.md) is authoritative.
