# Virgil TODO

## CI/CD — automated deploy to QNAP (replaces manual QSync + ssh docker build)

**Goal:** `git push` (or tag) → image built in CI → QNAP Container Station runs the new
version automatically. No QSync, no ssh, no building on the NAS. Rollback = previous image tag.

**Architecture (recommended): GitHub Actions → GHCR → Watchtower on QNAP**

```
git push main / tag v*
   └─ GitHub Actions: uv sync + ruff + pytest  (gate)
        └─ docker build --build-arg GIT_SHA=$GITHUB_SHA (linux/amd64)
             └─ push ghcr.io/<owner>/virgil:latest + :<sha> + :<tag>
QNAP (Container Station, behind Cloudflare Tunnel — unchanged)
   └─ watchtower (poll ~5 min, label-scoped)
        └─ pulls new :latest → recreates virgil → compose healthcheck (/healthz) gates it
```

**Why this variant:** zero inbound access to the NAS (tunnel stays outbound-only), no
self-hosted runner to maintain, Container Station treats it as plain containers, per-SHA
tags give instant rollback (`docker pull ...:<old-sha>` + retag), CI finally bakes GIT_SHA
correctly so the PWA cache busts on every deploy.

**Alternatives considered:**
- *QNAP cron `docker compose pull && up -d`* — fewest moving parts, but blind (pulls on a
  timer whether or not anything changed) and no update log. Fallback if Watchtower annoys.
- *Actions → ssh/cloudflared into QNAP* — inbound path + SSH secrets in GitHub; more surface, no gain.
- *Self-hosted runner on QNAP* — heavy, updates itself, overkill for one app.

**Deliverables:**
- [x] `.github/workflows/release.yml` — test gate → buildx → push to GHCR (`GITHUB_TOKEN`, `packages: write`); tags: `latest`, `sha-<short>`, `v*` on git tags; `ci.yml` narrowed to PRs/feature branches
- [x] `docker-compose.yml`: `image: ghcr.io/krzysztofbury/virgil:latest` (build: kept for local dev), `watchtower` service (label-scoped, 5-min poll, cleanup)
- [ ] One-time on QNAP: `docker login ghcr.io` with a `read:packages` PAT; remove the repo from QSync; copy the new compose + .env
- [x] README deploy section rewritten (registry flow, auto-deploy, force-update + rollback recipes)
- [ ] Optional: deploy notification (ntfy/Slack/WhatsApp) step in the workflow

**Out of scope for round 1:** staging environment, multi-arch images (QNAP is amd64), signed images.

**Deployment-semantics decision (2026-07-14):** keep Watchtower as **best-effort
auto-update** — NOT health-gated deploy with rollback. Mitigations in place: CI
test gate before every image, `concurrency` on the release workflow (no stale
`:latest` from slow runs), automatic **pre-migration DB snapshots** (migrations
are one-way; image rollback can't undo them), documented manual rollback via
per-commit `sha-*` tags. The alternative — a QNAP pull-and-verify script that
pins a digest, waits for `/healthz` and keeps the previous image reference — is
parked; revisit if a bad deploy actually bites.

## Backlog — 2026-07 Functionality Review

Status of the 2026-07 review (branch `fix/review-findings-2026-07`).

### P0 — Safety & dependability ✅ DONE (this branch)

- [x] Credential handling — `.qnap.setup` out of the Docker build context (**rotate the exposed LLM key + tunnel token**)
- [x] Factory Reset — new DB filename, registry repoint, migrated schema, back to onboarding
- [x] Multipart CSRF upload — medical-PDF onboarding unblocked, 20 MB limit unified
- [x] Oura OAuth (SameSite=Lax, Secure state cookie) + webhook routing (per-user callback URLs, spec-verified challenge GET + HMAC(client_secret, timestamp+body))
- [x] Private PWA cache — authenticated HTML never cached
- [x] Multi-user export isolation — filenames derived from account identity, never user-chosen
- [x] Legacy migration path — 007 rebuilds `llm_providers` before the claude→anthropic rename; upgrade test from a real pre-007 DB
- [x] P0 test coverage — signup/bootstrap, reset, OAuth callback state, multipart upload, multi-user isolation, webhook protocol, logout, SW cache privacy, migration upgrades (96 tests)

### P1 — Durable job model for LLM/sync/backup work

**Goal:** No user-facing request ever blocks on an LLM or Oura call; work survives restarts; no duplicate LLM cost.
**Plan:** Add a `jobs` table per user DB (id, kind, payload, status, attempts, last_error, created/finished). Scheduler loop doubles as the worker (claim → run → record). Onboarding enrichment, A.N.D.Y., experiment summaries, briefings, Oura sync, backup, export become job kinds. UI polls a lightweight `/api/jobs/{id}` partial via HTMX.
**Deliverables:** jobs table migration; worker in scheduler; onboarding progress screen with per-step status + retry + "continue without AI"; idempotency keys per (kind, date); tests for claim/retry/backoff.

### P1 — Recovery & data-ownership story

**Goal:** A user can fully restore their life data from an export/backup without SSH.
**Done already (2026-07-14):** backups enabled by default; timestamped filenames (hourly runs no longer overwrite one file); central `virgil-central.db` backed up daily by the scheduler; automatic pre-migration snapshots.
**Plan (remaining):** off-NAS copy (S3/rsync target); versioned export manifest (JSON, schema_version + all user tables); validated import endpoint (dry-run report → apply); restore-from-`.db`-upload in Settings > Data; backup age/status card; pre-reset backup download prompt; a documented restore drill.
**Deliverables:** `export/import` service with round-trip test (export → wipe → import → identical data); restore UI; backup freshness indicator on the Automation tab; docs.

### P1 — Mutation-feedback contract

**Goal:** Every write gives visible, accessible progress/success/failure — no silent redirects.
**Plan:** One helper pattern: disable control on submit, `aria-live` status region, persistent error toast with retry, `msg`/`err` params standardized across ALL pages (today only Settings renders them). Oura page sync (`/oura/api-sync`) currently swallows errors — add msg/err there first.
**Deliverables:** shared toast partial in `base.html`; msg/err rendering on every page; draft retention on network failure for daily notes/journal; tests asserting error surfacing for Oura sync + import.

### P1 — Multi-user hardening beyond a trusted household

**Goal:** Safe to give accounts to people you don't fully trust.
**Plan:** Password reset (email-less: admin-issued one-time reset codes); invite codes (admin panel, single-use, expiry) replacing the global open/closed switch; per-client API tokens (hashed at rest, scopes read/read-sensitive, revocable in Settings) replacing the single env key; API access log (who/what/when, no payloads).
**Deliverables:** `api_tokens` + `invites` central tables; token management UI; MCP server updated for per-token auth; audit view under Settings > Security; tests.

### P2 — Explicit offline behavior

**Goal:** Mobile users know exactly what works offline; no silent data loss.
**Plan:** Decide: offline **read-only** (persistent banner + disabled save controls when `navigator.onLine === false`) — cheap; or offline **capture** (IndexedDB queue + Background Sync + conflict policy) — expensive. Recommendation: read-only banner now, capture later only if real need appears.
**Deliverables:** offline banner + disabled mutations; SW keeps never caching authenticated HTML; docs updated to match.

### P2 — Edit/delete/audit paths for personal history

**Goal:** Sensitive records (relapses, blood results, goals, workouts) are correctable without SQL.
**Plan:** Add edit/delete endpoints + inline UI for blood results, pmo_events (with confirm + duplicate-date warning), goals (undo toast), workout sessions (already deletable — add per-entry edit); experiments day-detail sheet showing all activities per day.
**Deliverables:** routes + templates + validation, deletion confirmations with non-judgmental copy for Feniks, tests per entity.

### P2 — Longitudinal insights

**Goal:** Turn raw tracking into reflection.
**Plan:** 12-week daily heatmap (README already promises it), selectable ranges (4/12/26 weeks) for training + Oura trends, full life-score history list with detail view (diagnostic/priorities), Oura freshness badge ("last synced Xh ago") on dashboard + oura page.
**Deliverables:** range-parameterized queries + chart endpoints, life-score history page, freshness indicator, tests for range math.

### P2 — Accessibility

**Goal:** Usable with keyboard and assistive tech.
**Plan:** Replace clickable `<div>` menus (bottom bar) with `<button>` + `aria-expanded` + Escape handling; `aria-pressed` + visible state labels on three-state toggles; text/table equivalent for every chart; `prefers-reduced-motion` overrides; skip-to-content link; focusable help popovers instead of `title`-only tooltips.
**Deliverables:** base template + daily/oura/bloodwork template updates, CSS motion guards, axe-style smoke checklist in CONTRIBUTING.

### P3 — More wearables (after Oura webhook proves stable in prod)

**Goal:** Garmin/Apple Health/Google Fit import without multiplying fragile integrations.
**Plan:** Extract a `health_source` interface from the Oura sync (fetch window → normalized daily dict → `_upsert_daily`-style column groups per source); Garmin first (existing placeholder card).
**Deliverables:** source abstraction, Garmin OAuth + sync, per-source column ownership to avoid cross-source overwrites.

### P3 — i18n (EN + PL)

**Goal:** Full Polish UI, matching seeded Polish content.
**Plan:** Jinja2 `gettext` (`.po`/`.mo`) or JSON dict per locale; language setting per user; translate seed data (goal areas, milestones) via locale-aware seeds; date formatting per locale.
**Deliverables:** i18n plumbing, EN+PL catalogs, language selector in Settings, translated seeds, no hardcoded strings in templates (lint check).

### Deferred security/reliability (from reviews)

- [ ] Complete restore flow (part of the P1 recovery story above)
- [ ] Central DB migration system (today: `CREATE TABLE IF NOT EXISTS` only) — needed before more central schema changes
- [ ] Webhook subscription auto-renewal (Oura subscriptions carry `expiration_time`) + periodic reconciliation of subscription state
- [ ] Replay protection on webhook events (check `x-oura-timestamp` freshness)
- [ ] Pin CDN assets with SRI or self-host; move CSP off `unsafe-inline`/`unsafe-eval` (blocked by Alpine.js)
- [ ] Pin Docker base images by digest; `uv sync --frozen` in CI
- [x] Encrypt central TOTP secrets (done — lazy migration on next MFA enable)
- [x] SQLite `busy_timeout` on all connections (done)
- [x] Morning briefing scheduler task (done)

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
- [x] **MCP Server** — `mcp_server/virgil_mcp.py` (2026-07-05): thin stdio wrapper over the REST API
  - Tools: `get_today_summary`, `get_oura_stats`, `get_streaks`, `get_weekly_habits`, `get_experiments`, `get_training`
  - Runs anywhere (PEP 723 script, `uv run mcp_server/virgil_mcp.py`) — talks to the API over HTTPS, no local DB needed
  - Register: `claude mcp add virgil -e VIRGIL_API_URL=... -e VIRGIL_API_KEY=... -- uv run .../virgil_mcp.py`
  - Enables real-time, structured queries instead of stale markdown snapshots
- [x] **REST API** — JSON API endpoints for external tool access (`app/routers/api.py`, 2026-07-05)
  - `GET /api/summary` — today's dashboard data (energy, habits, Oura, Feniks streak, training week, measurements)
  - `GET /api/oura/today` — latest Oura vitals
  - `GET /api/habits?range=7` — habit completion data (1-90 days)
  - `GET /api/experiments/active` — active experiments with week target vs logged
  - `GET /api/training?range=7` — sessions with entries + volume (bonus, for Sunday reviews)
  - API key auth: `X-API-Key` vs `VIRGIL_API_KEY` env (constant-time), maps to `VIRGIL_API_USER_EMAIL` or first admin; read-only (GET only)
  - OpenClaw can call via HTTP on the Docker network (`http://virgil:8123/api/...`) or via tunnel
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
