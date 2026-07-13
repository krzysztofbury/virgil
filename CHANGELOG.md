# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed (post-review round 2 — TigerStyle + follow-up review)

- **Oura webhook protocol corrected against the live OpenAPI spec**: subscription management uses `x-client-id`/`x-client-secret` headers (was Bearer — every subscribe would have been rejected); verification is a GET challenge answered with `{"challenge": ...}`; event signatures verified as HMAC-SHA256(client_secret, timestamp + body), case-insensitive hex; event sync runs as a debounced background task inside Oura's 10-second response deadline; partial subscription coverage is surfaced to the user
- **Migration 007 no longer bricks legacy databases** — it rebuilds `llm_providers` without the provider CHECK before the claude→anthropic rename (upgrade test from a real pre-007 DB with a Claude row)
- **Factory reset provisions a NEW database filename** and repoints the central registry before deleting the old file — recreating at the same path raced live connections (SQLite WAL unlink hazard)
- **Markdown export filenames are derived from account identity** (primary keeps `virgil.md`, others get `virgil-{id}.md`) — user-chosen shared filenames allowed cross-user overwrite by construction
- Nested `<form>` removed from the Automation tab (Backup Now uses `formaction`) — invalid HTML that made Backup Now submit automation settings
- Bootstrap signup made atomic (guarded INSERT — two concurrent first signups can't both win); central account rolled back if user-DB provisioning fails
- Login with an empty password returns the normal error instead of 500; CSRF token comparison no longer 500s on non-ASCII input; webhook JSON shape validated pre-auth (no 500s)
- Startup migrations cover ALL users (disabled accounts no longer wake up with stale schemas)
- `/api/training/detail` groups sets by exercise id (duplicate names no longer merge) and returns `id`
- Central TOTP secrets Fernet-encrypted at rest (legacy plaintext migrates on next MFA enable); OAuth-state cookie gets `Secure` under HTTPS; `busy_timeout=5000` on every SQLite connection; `Retry-After` sleeps bounded to 60 s and parse-safe; `sync_log` included in JSON/CSV export

### Security

- **`.qnap.setup` excluded from the Docker build context** — the file can carry live deployment credentials (rotate any credentials that were in it)
- **Registration closed by default** (`VIRGIL_REGISTRATION_OPEN=false`); the first account (bootstrap owner) can always be created
- **Service worker no longer caches authenticated HTML** — dashboards/journals are no longer readable offline after logout
- **`/api/noporn` gated behind `VIRGIL_API_SENSITIVE=true`** (intimate journal content is opt-in)
- **Webhook secrets encrypted at rest**; CSRF tokens compared in constant time; login burns a dummy bcrypt verify for unknown emails (timing)
- Session cookie moved from `SameSite=Strict` to `Lax` so the Oura OAuth callback keeps its session (state-changing routes remain CSRF-protected)

### Fixed

- **Factory reset** no longer strands the account: the per-user DB is recreated and migrated, and the user is sent back to onboarding (previously: deleted DB + redirect to nonexistent `/setup`)
- **Multipart CSRF** — medical-PDF onboarding uploads were always rejected 403 (`parse_qs` cannot parse multipart); upload limits unified (20 MB)
- **Multi-user Oura webhooks** — per-user callback URLs (`/api/oura/webhook/{id}`) routed via a central registry instead of the retired global DB; subscriptions now register the handled data types (was `tag.updated`, which the handler ignored)
- **Partial Oura sync no longer erases data** — columns from failed endpoints keep their stored values instead of being overwritten with NULLs
- **Onboarding's suggested experiment is actually created** — targets go to `experiment_weeks` (+ a default activity type); previously the INSERT hit nonexistent columns and was silently swallowed
- **`llm_providers` CHECK constraint removed** (migration 012) — unblocks `anthropic`/`mistral`/`groq`/`ollama` providers and migration 007's rename
- **Internal LLM fallback recognized everywhere** — Daily A.N.D.Y. button and experiment summaries now work with only `VIRGIL_INTERNAL_LLM_KEY` set (`llm_available()`)
- **PWA icons committed** — the `Icon?` gitignore rule swallowed `app/static/icons/` on case-insensitive filesystems, breaking SW install on fresh clones
- **Backup Now** reports the real outcome (was an HTMX fire-and-forget that showed "started" even on failure)
- Deleting a training exercise archives it instead of erasing all its historical entries and PBs (migration 013)
- Empty and negative workout submissions are rejected server-side
- Bloodwork: out-of-range flags computed from reference ranges (manual override still wins); unknown marker ids no longer 500
- Dashboard radar only plots complete life-score assessments (missing areas rendered as fake zeros)
- Experiments: inverted weekly targets normalized at creation; completed/abandoned experiments can be reopened; start date prefilled

### Added

- **Per-user markdown export filename** (Settings > Data) — multi-user deployments no longer overwrite each other's `virgil.md`
- **Scheduled morning briefing** — the existing Automation toggle now actually generates the briefing once per day (after 06:00, 1 h failure backoff)
- **`/healthz` endpoint** (503 while any user DB failed startup migrations) — wired into the Docker healthcheck
- JSON/CSV export now includes `user_profiles`, `experiment_weeks`, `experiment_summaries`, `daily_briefings`, `exercise_library`, `app_settings`
- `/api/training/detail` batches entry queries (N+1 removed)

## [0.2.0] - 2026-03-21

### Added

- **Multi-user architecture** with per-user isolated SQLite databases (`data/users/{uuid}.db`)
- **Central auth service** — user registry in `virgil-central.db`, signup/login against central DB
- **Signup page** (`/signup`) with email, display name, password — creates user + per-user DB
- **Admin panel** (`/admin/users`) — list, disable, enable, delete users (admin role required)
- **Admin role system** — super-admins via `VIRGIL_ADMIN_EMAILS` env var, promotable via admin panel
- **Registration control** — `VIRGIL_REGISTRATION_OPEN` env var to open/close signups
- **6-step onboarding wizard** (`/onboarding`) — profile, ideal day, goals, habits, medical records
- **LLM-powered onboarding enrichment** — generates realistic day, goal levels (10yr/3yr/1yr), experiment suggestion, Feniks auto-detection
- **LiteLLM integration** — unified LLM provider access replacing hand-rolled HTTP clients
- **Internal LLM provider** — `VIRGIL_INTERNAL_LLM_MODEL` + `VIRGIL_INTERNAL_LLM_KEY` for system features
- **Expanded LLM provider dropdown** — Anthropic, OpenAI, Gemini, Mistral, Groq, Ollama, Other (LiteLLM)
- **Medical record import** — PDF upload via multimodal LLM or free-text parsing into blood markers
- **Factory reset** in Settings > Security — wipes user DB for fresh start
- **Migration script** (`scripts/migrate_to_multiuser.py`) — converts single-user installs to multi-user

### Changed

- Auth middleware rewritten for multi-user (UUID sessions, per-user DB per request)
- All routers now use `get_user_db_from_request(request)` instead of global `get_db()`
- Scheduler iterates over all active users for per-user tasks
- Feature flags loaded per-user instead of global cache
- Typography upgraded from Inter to DM Sans + JetBrains Mono
- Color palette replaced with custom "Midnight Observatory" theme (teal accent #2cb67d)
- Stat values use solid color + mono font instead of gradient text

### Security

- bcrypt pre-hashed with SHA-256 to prevent 72-byte truncation
- SQL injection prevented via column whitelist in `update_user`
- Path traversal guard on per-user DB filenames
- `/signup` added to rate limiter auth tier (10 req/min)
- UUID format validation on session payloads
- Admin self-disable/self-delete prevention
- HSTS header when behind HTTPS
- `CF-Connecting-IP` used for rate limiting behind Cloudflare

---

## [0.1.0] - 2026-03-21

### Added

- **Dashboard** with weekly completion stats, life score radar chart, Oura vitals, 7-day sparklines, year calendar, and AI morning briefing
- **Daily Log** with energy tracking, morning/evening routines, A.N.D.Y. task system (AI-generated daily tasks), body measurements, markdown notes, streak counters, and 12-week heatmap
- **Training** with 4-section exercise protocol (Warmup/Core/Cardio/Stretching), exercise CRUD, section-specific logging, KPI cards, personal bests, rest timer
- **Feniks** 90-day personal development program tracker with streak hero, progress graph, journal, pleasures, and milestones
- **Oura Ring Integration** with OAuth2 connection, automatic daily sync, real-time webhook support (HMAC-SHA256 verified), rate limit handling
- **Bloodwork** tracking with marker categories, reference ranges, flag indicators, trend charts
- **Life Scores** periodic self-assessment across 8 life areas with radar chart
- **Goals** mapping across 8 life areas with 1yr/3yr/10yr horizons and inline editing
- **Experiments** with time-boxed activities, weekly targets, color-coded activity types, day-by-day grid, AI weekly summaries, Oura workout auto-import
- **Settings** with 5-tab layout (General, Integrations, Data, Automation, Security)
- **Authentication** with email + password (bcrypt), optional TOTP MFA, signed cookie sessions (7-day expiry)
- **Security middleware** — CSRF protection, rate limiting (120/min general, 10/min auth), security headers (CSP, X-Frame-Options, etc.)
- **Encryption at rest** for OAuth tokens, LLM API keys, and webhook secrets (Fernet)
- **Database migration system** with 6 versioned migrations
- **Background scheduler** for automated backups, Oura sync, and markdown export
- **Markdown export** with scoped output (weekly/monthly/yearly/all) for LLM-based reviews
- **Markdown import** for bootstrapping from existing Second Brain files
- **LLM integration** supporting Claude, OpenAI, and Gemini APIs
- **PWA support** with service worker (cache-first for static, stale-while-revalidate for CDN, network-first for pages)
- **Dark/light theme** with localStorage + server sync, theme-aware charts
- **Keyboard shortcuts** with `g`-prefix navigation and `?` help overlay
- **Swipe gestures** for mobile day navigation
- **Docker deployment** with Cloudflare Tunnel support for QNAP NAS

### Security

- Fixed OAuth callback missing `state` parameter (CSRF protection)
- Migrated LLM API keys from plaintext to Fernet encryption
- Added CSRF double-submit cookie protection on all POST forms
- Secured encryption key file permissions (0600)
- Required HMAC signature verification on webhook payloads
- Removed public MFA QR endpoint, validated URI scheme
- Fixed rate limiter memory leak with bucket eviction + 10K IP cap
- Replaced unstable `_signing_key` access with SHA-256 key derivation
- Added input length limits on all text fields
- Moved Gemini API key from URL query param to header
- Fixed XSS via exercise name in `confirm()` dialogs

### Fixed

- Dashboard, training, bloodwork, and experiments N+1 query patterns
- Hardcoded years/dates replaced with dynamic computation
- Variable shadowing in for loops
- Experiment summary LLM cooldown (5-minute per-experiment)
- Single DB connection health check with reconnection
- KPI volume calculation filtered to Core section only
