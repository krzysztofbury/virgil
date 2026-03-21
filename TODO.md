# Virgil TODO

## Code Review Findings (TigerStyle Audit)

### Critical — Security & Data Integrity

- [x] **OAuth callback missing `state` parameter** — Added CSRF-safe state via cookie validation.
- [x] **LLM API keys stored in plaintext** — Now Fernet-encrypted at rest with auto-migration.
- [x] **No CSRF protection on any POST form** — Double-submit cookie middleware + auto-injected tokens.
- [x] **Encryption key file written with default permissions** — `os.open()` with `0o600`.

### High — Bugs & Correctness

- [x] **Stress column mapping in oura_monthly recompute** — Confirmed correct, added clarifying comments.
- [x] **`total("steps")` called twice per month** — Bound to local variable.
- [x] **Duplicate streak calculation — 3 copies** — Uses shared `get_streak()` service everywhere.
- [x] **Unhandled ValueError on `/daily/{day}` with bad date** — try/except with redirect.
- [x] **No date validation on Form(date)** — Added fromisoformat validation on daily.py, feniks.py.
- [x] **`contextlib.suppress(Exception)` silently swallows sync errors** — All 9 replaced with logged try/except.
- [x] **No timeout on OAuth HTTP calls** — `timeout=30.0` on all httpx clients.

### Medium — Performance & Architecture

- [x] **Dashboard N+1 query** — Single `BETWEEN` query, grouped in Python.
- [x] **Training page N+1 query** — Single JOIN, entries grouped by session_id.
- [x] **Bloodwork N+1 query** — Single `WHERE marker_id IN (...)` batch query.
- [x] **Experiments list N+3 queries per experiment** — 3 batch queries replace per-experiment loop.
- [x] **`import_liczby` hardcodes year 2026** — Parses year from file/week headers, falls back to current year.
- [x] **`sync_noporn` hardcodes end date "05.05.2026"** — Computed from `start_date + target_days`.
- [x] **`sync_cele` hardcodes horizon years** — Computed from `date.today().year`.
- [x] **`docker-compose.yml` missing new env vars** — Added `VIRGIL_ENCRYPTION_KEY` and `VIRGIL_BASE_URL`.
- [x] **`_fetch_endpoint` doesn't differentiate 401** — Raises `OuraAuthError` on 401, marks integration as error.

### Low — Code Quality & DX

- [x] **`delete_experiment` manually cascades despite ON DELETE CASCADE** — Removed redundant child deletes.
- [x] **`goal_id: int | None` truthy check fails on 0** — Changed to `if goal_id is not None:`.
- [x] **`_MONTHS` uses English, rest of app uses Polish** — Full UI rewritten to English. Multilanguage support planned.
- [x] **`_md_inline` regex fragile for nested markup** — Extracted `_apply_inline_md()` handling `***bold italic***` before `**bold**` before `*italic*`.
- [x] **`import_oura` builds SQL with f-strings** — Added column name allowlist validation.
- [x] **`PORT` config crashes on non-integer** — Wrapped in try/except with default.
- [x] **`Dockerfile` copies `scripts/` directory** — Removed (not needed at runtime).
- [x] **`float == int(float)` for display formatting** — Uses `float.is_integer()`.

## Code Review Findings (TigerStyle Audit #2)

### Critical — Security

- [x] **Webhook signature verification optional** — HMAC check now required on all non-verification requests.
- [x] **MFA QR endpoint abuse** — Removed `/mfa/qr.png` from PUBLIC_PATHS, validates `otpauth://` URI scheme.
- [x] **Rate limiter memory leak** — Added bucket eviction for stale IPs + 10K cap to prevent OOM.
- [x] **Session signing key from private attribute** — Replaced `_signing_key` access with stable `get_signing_key()` via SHA-256 derivation.
- [x] **No input length limits on text fields** — Added `truncate()` helper, applied to all text form fields across all routers.
- [x] **Gemini API key exposed in URL** — Moved from query param to `x-goog-api-key` header.

### High — Bugs & Correctness

- [x] **`oura_monthly` stress_normal always 0** — Added clarifying comments; stress_low doesn't exist in Oura API v2.
- [x] **Auth middleware DB query on every request** — Cached `_user_exists` in module global, reset on setup.
- [x] **Feature flags DB query on every request** — Cached with 30s TTL, invalidated on save.
- [x] **Dashboard loads ALL life scores** — Added `LIMIT 2` (only 2 used for radar chart).
- [x] **Variable shadowing in for loops** — Fixed `for x in rows: x = dict(x)` patterns in oura_api, briefing, dashboard.

### Medium — Performance & Architecture

- [x] **Experiment summary LLM call on every page load** — Added 5-minute per-experiment cooldown.
- [x] **Markdown export N+1 for experiments** — Batch-loaded entries and summaries with `IN (...)`.
- [x] **Single DB connection with no health check** — Added `SELECT 1` reconnection on dead connection.
- [x] **Duplicate AREAS/AREA_LABELS constants** — Extracted to `app/db.py` as shared `LIFE_AREAS`/`LIFE_AREA_LABELS`.

### Low — Code Quality & DX

- [x] **Webhook body parsed twice without comment** — Added clarifying comments.
- [x] **SCHEMA constant doesn't match actual schema** — Added migration column documentation note.
- [x] **Inconsistent httpx timeout types** — Normalized all to `float` (60.0, 30.0).

## Code Review Findings (TigerStyle Audit #3 — Training Overhaul)

### Critical — Security

- [x] **XSS via exercise name in `confirm()` dialog** — Jinja2 doesn't escape single quotes; crafted exercise name breaks JS in `onclick="confirm('Delete {{ ex.name }}?')"`. Fixed: removed user data from inline JS, use static string.

### High — Bugs & Correctness

- [x] **KPI volume included all sections** — Template labeled "Volume (Core)" but query summed all entries. Fixed: `CASE WHEN tex.section = 'Core'` filter in SQL.
- [x] **Dead query result** — `week_stats` session_count computed via inflated JOIN, immediately overwritten by correct separate query. Fixed: removed dead query, kept correct one.

### Low — Code Quality & DX

- [x] **No `sets` upper bound in UI** — Alpine.js `sets++` unbounded; backend silently drops sets > 10. Fixed: `if(sets < 10) sets++`.

---

## Oura Integration Polish
- [x] Scheduled auto-sync (background task every 6h instead of manual "Sync Now")
- [x] Flash messages / toast notifications for sync success/failure
- [x] Oura daily data table on `/oura` page (browsable 30-day history)
- [x] Daily Oura trends chart (10-day daily granularity, dual-axis HRV/RHR + scores)
- [x] Handle Oura API rate limits gracefully (429 → exponential backoff with Retry-After)
- [x] Show token expiry status on Settings page

## Dashboard Improvements
- [x] Sparkline mini-charts for Oura metrics on dashboard (7-day HRV/sleep trend)
- [x] Weekly energy trend chart (from daily_logs)
- [x] Dashboard "morning briefing" — AI-generated summary of today's state (sleep quality, streak, tasks)

## Integrations Framework
- [x] Generic integration status page (list all connected services)
- [x] Webhook support for real-time Oura updates (instead of polling)
- [x] Auto-export virgil.md to Second Brain on schedule (Settings > Automation, configurable interval)
- [ ] **MCP Server** — Expose Virgil data via Model Context Protocol for OpenClaw and other AI agents
  - Tools: `get_today_summary`, `get_oura_stats`, `get_streaks`, `get_weekly_habits`, `get_experiments`
  - Runs as an additional service in docker-compose or embedded in the FastAPI process
  - OpenClaw connects as MCP client over stdio or SSE
  - Enables real-time, structured queries instead of stale markdown snapshots
- [ ] **REST API** — JSON API endpoints for external tool access
  - `GET /api/summary` — today's dashboard data (energy, habits, Oura, streaks)
  - `GET /api/oura/today` — latest Oura vitals
  - `GET /api/habits?range=7d` — habit completion data
  - `GET /api/experiments/active` — active experiment progress
  - API key auth (separate from session auth) for machine-to-machine access
  - OpenClaw can call via HTTP on the Docker network (`http://virgil:8123/api/...`)
- [ ] Garmin Connect integration (for users with Garmin instead of Oura)
- [ ] Google Fit / Apple Health import

## Training
- [x] Progressive overload tracking (12-week per-exercise weight/reps sparklines) — replaced with Personal Bests KPI cards
- [x] Training volume chart (8-week aggregated bar chart) — replaced with This Week KPI stat cards
- [x] Rest timer during workout logging
- [x] Training overhaul — 4 sections (Warmup/Core/Cardio/Stretching), English names, equipment-focused exercises
- [x] Exercise CRUD — add/edit/delete exercises inline per section
- [x] Section-specific logging — warmup (toggle+duration), core (sets×reps+kg), cardio (rounds+duration), stretching (duration)
- [x] KPI stat cards — sessions, volume (Core only), total reps, personal bests (12-week max weight)

## Daily Log
- [x] Streak tracking for individual habits (7 habits, reverse-chronological scan)
- [x] Weekly/monthly habit completion heatmap (12-week CSS grid)
- [x] Notes with markdown rendering (Alpine.js edit/preview toggle)

## Feniks
- [x] Progress graph (streak timeline with red dots at relapse points)

## Data & Reliability
- [x] Automated daily backup (SQLite → `data/backups/` with rolling retention)
- [x] Data export to JSON/CSV (all 21 tables, download endpoints)
- [x] Markdown export with selectable sections
- [x] Migration system for DB schema changes (instead of CREATE IF NOT EXISTS)
- [x] Input validation on all forms (shared `validation.py` helpers, all POST endpoints covered)

## Settings & Infrastructure
- [x] **Settings page restructured into 5 tabs** (General, Integrations, Data, Automation, Security) with HTMX tab switching
- [x] **`app_settings` key-value table** — shared foundation for all configurable features
- [x] **Background scheduler** (`app/services/scheduler.py`) — asyncio loop for backup + Oura auto-sync
- [x] **Automation settings** — backup enable/interval/retention, Oura auto-sync enable/interval, briefing toggle

## UI/UX
- [x] PWA offline support (service worker + cache)
- [x] Swipe gestures for day/week navigation on mobile
- [x] Keyboard shortcuts (g-prefix navigation, arrow keys, ? help overlay)
- [x] Dark/light theme toggle
- [ ] **Multilanguage support (i18n)** — Extract all UI strings into translation files (JSON/YAML per locale). Support at minimum EN + PL. Approach options:
  - Jinja2 `gettext` integration with `.po`/`.mo` files (standard Flask pattern)
  - Simple JSON-based translation dict loaded per locale, injected into template context
  - Language selector in Settings, stored in DB or cookie
  - Translate seed data (goal area names, milestone titles) — these are currently Polish in DB seeds

## Onboarding
- [x] **6-step onboarding wizard** — LLM-assisted setup at `/onboarding` with profile, ideal day, goals, habits, medical records
- [x] LLM enrichment — generates realistic day, goal levels (10yr/3yr/1yr), experiment suggestion, Feniks auto-detection
- Current `import_all()` in `app/services/markdown_import.py` handles markdown import for bootstrapping

## Multi-User & SaaS (Future)
- [ ] **Encrypted backup to S3** — per-user DB encrypted and backed up to S3 bucket or local folder on schedule
  - Per-user Fernet key derived from master key + user UUID
  - Configurable: S3 bucket, local path, backup interval
  - Restore from backup flow in admin panel
- [ ] **Subscription/Billing** — Stripe integration for paid tiers
  - Free tier: local storage, internal LLM only
  - Pro tier: cloud backup, all LLM providers, priority support
  - Stripe webhook handler for subscription lifecycle
  - Tier enforcement in middleware (check subscription status per request)
- [ ] **Admin panel: Impersonate user** — view a user's dashboard as them (for support)
- [ ] **Admin panel: Invite codes** — restrict registration to invite-only mode
- [ ] **Admin panel: Usage stats** — storage per user, LLM calls, last activity
- [ ] **Admin panel: Force password reset** — admin can force a user to change password on next login

## Security & Authentication
- [x] **Authentication system** — Email + password + TOTP MFA (Option A)
  - Signed cookie sessions via `itsdangerous.TimestampSigner` (7-day expiry)
  - `AuthMiddleware` protects all routes except `/login`, `/setup`, `/mfa/verify`
  - MFA setup/disable in Settings, QR code generation via `pyotp` + `qrcode`
  - MFA-pending sessions blocked from protected routes
- [x] Migrate LLM API keys to Fernet encryption (currently plaintext)
- [x] Rate limiting on API endpoints (120/min general, 10/min auth — sliding window per IP)
- [x] CSRF protection on all POST forms
- [x] Security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy)
