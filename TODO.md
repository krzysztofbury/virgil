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

## Training page simplified — 2026-08-01 (branch `feat/training-page-simplify`)

**Closed 2026-08-27** by v0.6.0 Phase A (branch `feat/v0.6.0-training-wod-ui`,
13 commits, plan in `docs/superpowers/plans/2026-08-27-v0.6.0-training-wod-blockers.md`).
Every item below is ticked or carries a written decision in the code.

Raised by the final whole-branch review (5 rounds; each earlier round was scoped
to the fix commits, which is why every defect kept turning up at a seam with code
the reviewer had not been given). Ranked, none blocking:

- [x] **Duration stored in seconds, rendered as minutes** — fixed 2026-08-02.
      Migration 020 converts the legacy minute rows by a structural rule (which
      branch of the deleted log form wrote them), verified row by row against the
      deployed database. `format_duration_seconds` renders the column.
- [x] The markdown export still drops `duration` entirely (`_section_training`
      renders Exercise/Set/Reps/Weight), so it now omits the one column that just
      got a canonical unit.
- [x] **A partial resolve consumes the session silently.** `confirm_wod` skips
      rows whose movement no longer resolves and the `if not rows:` guard is
      all-or-nothing, so 1-of-2 rows can be written with no message and no route
      back. Same family as the four blockers, narrower trigger.
- [x] **A crash between `capture_wod`'s two commits leaves a session that can
      never gain entries** — note saved, `wod_parsed` NULL, no form, no `dokończ`.
      Not a regression (the deleted log form always INSERTed a new session), and
      Watchtower recreating the container mid-parse is the realistic trigger. Fix
      is read-side: offer manual entry for an entry-less session with notes.
- [x] **A parse over 200 entries can never be saved** — `entry_count` is capped at
      200 and the parser bounds nothing. Only exit is discarding the parse.
- [x] **A rejected field silently discards every other edit in the form,**
      including rows added with „+ dodaj serię". The toast names one field and
      says nothing about the rest.
- [x] **Double-click on „Zapisz i sparsuj" creates two sessions and two paid LLM
      calls.** PRG protects the confirm page, not capture.
- [x] **Sessions outside the newest 20 by date are unreachable** — `/training` is
      `ORDER BY date DESC LIMIT 20`, so backdating a capture hides it and its
      `dokończ` immediately, while the confirm screen promises it is visible there.
- [x] **`training_entries.notes` has no reader.** The confirm screen collects it
      and the parser prompt designates it for the metcon result; history, the API,
      MCP and the markdown export all drop it.
- [x] **The planner is given a swim target it cannot score.** Nothing distinguishes
      a swim from a WOD in the logged sessions it reads.
- [x] **`DEFAULT_DAYS`/`DEFAULT_SWIM` make "never configured" look like a
      configured CrossFit week** for any new user. `normalize_days("")` already
      renders "No fixed CrossFit days set." — the honest default is `""`.
- [x] `training_exercises.ad_hoc` now has no reader, and `archived` there can only
      be cleared, never set. Neither is a bug; both will mislead the next reader.
- [x] The `?err=` toast is interpolated into a JS string literal — no XSS
      (autoescape holds), but a backslash silently truncates the message. `|tojson`.


Removed the protocol table, the per-set log form and the rest timer; free-text
capture is the only input. The A.N.D.Y. planner now reads a weekly schedule
(`app/services/training_schedule.py`, stored in `app_settings`) instead of a
per-exercise prescription drawn from `training_exercises` — those rows outlive
the program that created them, so deleting only the UI would have hidden the
staleness rather than fixed it.

- [x] `exercise_library.sets` / `reps` / `display_order` fed the deleted
      protocol form. They still populate the parser's fallback values and the
      Settings listing, but nothing consumes them as a prescription any more.
      Worth deciding whether they stay or go before the next library change.
- [x] The schedule is CrossFit/swim-shaped in its wording (`CrossFit days:`).
      Fine while that is the training, but it is copy, not data — generalise it
      if the sport changes rather than editing the day list around it.

## CrossFit WOD tracking — 2026-07-30 (branch `feat/crossfit-wod-tracking`, PR #6)

Shipped: free-text WOD capture (note persisted before the LLM runs), parsing
confined to a closed movement vocabulary, editable confirmation screen, `ad_hoc`
movements, a user-curatable dictionary over the Settings form / REST / MCP, and a
single shared write contract for that dictionary. Migrations 016–018.

- [x] Free-form tags replacing `category`, names unique library-wide (migration
      019, branch `feat/exercise-tags`) — `?tag=` filter + MCP tag parameters,
      section-grouped listing in Settings → App Config, ASCII transliteration so
      Polish tags fold instead of vanishing. See CHANGELOG. (The tag-filter chips
      this also shipped lived in the Training picker, which was removed the next
      day — tags remain in Settings, the API and the parser vocabulary.)

Deferred deliberately — each was raised by a review, ruled non-blocking, and is
recorded here so it isn't rediscovered from scratch:

- [x] `/settings/library/archive` routed through `validate_library_write`
      (PR #9) — an unknown id now redirects with an error instead of silently
      no-opping, matching `PATCH /api/library/{id}`.
- [x] A parse yielding more than 200 entries produces a confirmation screen that
      rejects on every submit, with no user-adjustable input. Not a regression
      (the previous behaviour discarded them silently) but it is an
      unrecoverable loop. Either cap the parse or let the screen paginate.
- [x] `training_exercises.name` is unique case-insensitively (migration 026),
      legacy race duplicates retain their entries, and `resolve_movement` uses
      an atomic upsert. Semantic conflicts abort instead of rewriting history.
- [x] Admin and session UUID validation use one canonical UUID helper; trailing
      newlines are rejected before an admin action or central DB lookup.
- [x] `CF-Connecting-IP` is trusted only when explicitly enabled and the direct
      peer appears in `VIRGIL_TRUSTED_PROXY_IPS`; direct ingress ignores spoofed
      forwarding headers. Compose defaults stay disabled.
- [x] `client` and `auth_client` now have independent cookie jars on the same
      application lifespan portal, with a regression proving anonymous state
      does not inherit the authenticated session.

**Note on test quality.** Twelve vacuous tests were caught and fixed during this
work — tests that passed under every mutation of the code they claimed to cover.
Recurring shapes: asserting on a field name built at runtime by Alpine.js and
never present in server-rendered HTML; asserting on a substring the template
emits regardless (a movement name rendered as an `<option>` in every row); and
the shared-fixture problem above. The countermeasure that worked was requiring
mutation evidence from someone other than the test's author — worth keeping for
future work in this repo.

## Backlog — 2026-07 Functionality Review

Status of the 2026-07 review (branch `fix/review-findings-2026-07`).

**Implementation roadmap (2026-08-27):**
[`docs/superpowers/plans/2026-08-27-reliability-data-ownership-roadmap.md`](docs/superpowers/plans/2026-08-27-reliability-data-ownership-roadmap.md).
The order below is intentional: trustworthy tests and central migrations come
before jobs, restore, or new identity tables. Phase 9 is conditional on an
explicit decision to support accounts outside the trusted household.

- [x] **Phase 0 — Test isolation and small integrity gaps:** independent
      anonymous/authenticated clients, strict admin UUID validation, explicit
      trusted-proxy handling, atomic unique training movement resolution.
      Completed on `fix/reliability-phase-0-foundations`; 530 tests and
      TigerStyle pair-programming review passed before push.
- [x] **Phase 1 — Central DB migrations:** numbered central schema, automatic
      pre-migration snapshot, health-check failure on migration failure.
      Completed on `fix/reliability-phase-1-central-migrations`; atomic schema
      validation/version stamping, fail-closed startup quarantine, 548 tests and
      three-round TigerStyle pair-programming review passed before push.
- [ ] **Phase 2 — Mutation-feedback contract:** shared accessible progress,
      success and persistent failure UI across every write route.
- [ ] **Phase 3 — Durable jobs:** restart-safe queue for LLM, sync, backup and
      export work, with bounded retries and no automatic retry at an ambiguous
      paid-LLM boundary.
- [ ] **Phase 4 — Oura lifecycle hardening:** timestamp replay protection,
      subscription expiry tracking, renewal and periodic reconciliation.
- [ ] **Phase 5 — Recovery and data ownership:** versioned JSON transfer,
      validated `.db` restore, freshness status, off-NAS copy and restore drill.
- [ ] **Phase 6 — Explicit offline and accessible shell:** read-only offline
      state, disabled writes, keyboard-safe global navigation, reduced motion
      and chart text/table equivalents.
- [ ] **Phase 7 — Correctable history and audit:** blood results, PMO events,
      goals, workout entries and experiment-day details editable without SQL.
- [ ] **Phase 8 — Longitudinal insights:** real 12-week Daily heatmap, common
      4/12/26-week ranges, Life Score history and Oura freshness.
- [ ] **Phase 9 — Multi-user hardening (conditional):** invites, one-time reset
      codes, scoped per-client API tokens and metadata-only access audit.

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

**Roadmap status:** Phase 3, blocked by Phases 0 and 2.
**Goal:** No user-facing request ever blocks on an LLM or Oura call; work survives restarts; no automatic duplicate LLM cost at an ambiguous provider boundary.
**Plan:** Add a `jobs` table per user DB (id, kind, payload, status, attempts, last_error, created/finished). Scheduler loop doubles as the worker (claim → run → record). Onboarding enrichment, A.N.D.Y., experiment summaries, briefings, Oura sync, backup, export become job kinds. UI polls a lightweight `/api/jobs/{id}` partial via HTMX.
**Deliverables:** jobs table migration; worker in scheduler; onboarding progress screen with per-step status + retry + "continue without AI"; idempotency keys per (kind, date); tests for claim/retry/backoff.

### P1 — Recovery & data-ownership story

**Roadmap status:** Phase 5, blocked by central migrations, mutation feedback and durable jobs.
**Goal:** A user can fully restore their life data from an export/backup without SSH.
**Done already (2026-07-14):** backups enabled by default; timestamped filenames (hourly runs no longer overwrite one file); central `virgil-central.db` backed up daily by the scheduler; automatic per-user pre-migration snapshots.
**Plan (remaining):** off-NAS copy (S3/rsync target); versioned export manifest (JSON, schema_version + all user tables); validated import endpoint (dry-run report → apply); restore-from-`.db`-upload in Settings > Data; backup age/status card; pre-reset backup download prompt; a documented restore drill.
**Deliverables:** `export/import` service with round-trip test (export → wipe → import → identical data); restore UI; backup freshness indicator on the Automation tab; docs.

### P1 — Mutation-feedback contract

**Roadmap status:** Phase 2, after test-fixture isolation.
**Goal:** Every write gives visible, accessible progress/success/failure — no silent redirects.
**Plan:** One helper pattern: disable control on submit, `aria-live` status region, persistent error toast, `msg`/`err` params standardized across ALL pages (today only Settings renders them), and retry links only for idempotent operations. Oura page sync (`/oura/api-sync`) currently swallows errors — add msg/err there first.
**Deliverables:** shared toast partial in `base.html`; msg/err rendering on every page; draft retention on network failure for daily notes/journal; tests asserting error surfacing for Oura sync + import.

### P1 — Multi-user hardening beyond a trusted household

**Roadmap status:** Conditional Phase 9. Do not start without an explicit product decision.
**Goal:** Safe to give accounts to people you don't fully trust.
**Plan:** Password reset (email-less: admin-issued one-time reset codes); invite codes (admin panel, single-use, expiry) replacing the global open/closed switch; per-client API tokens (hashed at rest, scopes read/read-sensitive, revocable in Settings) replacing the single env key; API access log (who/what/when, no payloads).
**Deliverables:** `api_tokens` + `invites` central tables; token management UI; MCP server updated for per-token auth; audit view under Settings > Security; tests.

### P2 — Explicit offline behavior

**Roadmap status:** Phase 6. Decision made: read-only offline, no mutation queue.
**Goal:** Mobile users know exactly what works offline; no silent data loss.
**Plan:** Ship offline **read-only** now: persistent banner + disabled save controls when `navigator.onLine === false`. Defer offline capture (IndexedDB queue + Background Sync + conflict policy) until real usage proves it is needed.
**Deliverables:** offline banner + disabled mutations; SW keeps never caching authenticated HTML; docs updated to match.

### P2 — Edit/delete/audit paths for personal history

**Roadmap status:** Phase 7, after self-service recovery exists.
**Goal:** Sensitive records (relapses, blood results, goals, workouts) are correctable without SQL.
**Plan:** Add edit/delete endpoints + inline UI for blood results, pmo_events (with confirm + duplicate-date warning), goals (undo toast), workout sessions (already deletable — add per-entry edit); experiments day-detail sheet showing all activities per day.
**Deliverables:** routes + templates + validation, deletion confirmations with non-judgmental copy for Feniks, tests per entity.

### P2 — Longitudinal insights

**Roadmap status:** Phase 8, after history corrections and accessible chart equivalents.
**Goal:** Turn raw tracking into reflection.
**Plan:** 12-week daily heatmap (README already promises it), selectable ranges (4/12/26 weeks) for training + Oura trends, full life-score history list with detail view (diagnostic/priorities), Oura freshness badge ("last synced Xh ago") on dashboard + oura page.
**Deliverables:** range-parameterized queries + chart endpoints, life-score history page, freshness indicator, tests for range math.

### P2 — Accessibility

**Roadmap status:** Phase 6 for the app shell, completed per-page during Phases 7 and 8.
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
- [x] Flash messages / toast notifications for Settings sync success/failure
- [ ] Apply the shared mutation-feedback contract to `/oura/api-sync` and every
      remaining Oura write path (roadmap Phase 2)
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
- [ ] Weekly/monthly habit completion heatmap (12-week CSS grid) — current
      implementation renders only the selected 7-day window; roadmap Phase 8
- [x] Notes with markdown rendering (Alpine.js edit/preview toggle)

## Feniks
- [x] Progress graph (streak timeline with red dots at relapse points)

## Data & Reliability
- [x] Automated daily backup (SQLite → `data/backups/` with rolling retention)
- [x] Data export to JSON/CSV (all 21 tables, download endpoints)
- [x] Markdown export with selectable sections
- [x] Migration system for per-user DB schema changes (instead of CREATE IF NOT EXISTS)
- [x] Migration system for `virgil-central.db` (roadmap Phase 1)
- [x] Input validation on all forms (shared `validation.py` helpers, all POST endpoints covered)

## Settings & Infrastructure
- [x] **Settings page restructured into 5 tabs** (General, Integrations, Data, Automation, Security) with HTMX tab switching
- [x] **`app_settings` key-value table** — shared foundation for all configurable features
- [x] **Background scheduler** (`app/services/scheduler.py`) — asyncio loop for backup + Oura auto-sync
- [x] **Automation settings** — backup enable/interval/retention, Oura auto-sync enable/interval, briefing toggle

## UI/UX
- [x] PWA install/static-asset cache and public `/offline` fallback; authenticated
      HTML remains correctly network-only
- [ ] Explicit read-only offline state with a persistent banner and disabled
      mutation controls (roadmap Phase 6)
- [x] Swipe gestures for day/week navigation on mobile
- [x] Keyboard shortcuts (g-prefix navigation, arrow keys, ? help overlay)
- [x] Dark/light theme toggle
- [ ] **Multilanguage support (i18n)** — Extract all UI strings into translation files (JSON/YAML per locale). Support at minimum EN + PL. Approach options:
  - Jinja2 `gettext` integration with `.po`/`.mo` files (standard Flask pattern)
  - Simple JSON-based translation dict loaded per locale, injected into template context
  - Language selector in Settings, stored in DB or cookie
  - Translate seed data (goal area names, milestone titles) — these are currently Polish in DB seeds

### UI/UX audit - 2026-08-27

Browser audit on fictional demo data: 11 primary routes captured at 1440x1000 and
390x844 (22 screenshots total). No page-wide horizontal overflow, console errors
or JavaScript errors. Visual inspection still found content clipped inside
containers with `overflow: hidden`, controls hidden in internally scrolling
tables, and several mobile flows whose primary action is buried below analytics.

**Decisions (2026-08-27, user):** the Goals focus set is SOFT - it warns above
three starred goals and blocks nothing, so no goal can be lost to a cap. The
Dashboard hero is the next action (check-in CTA plus the A.N.D.Y. list), not a
readiness-versus-baseline interpretation.

**Product direction:** use the redesigned Feniks page as the interaction model
for the rest of Virgil: show the meaningful outcome first, ask one current
question, reveal conditional details only when needed, and put history last.
Do not make every metric, form and historical view equally prominent.

#### P0 - mobile usability defects

- [x] **Training weekly KPIs are clipped on mobile.** The template forces three
      columns inline while `.card` hides overflow, so the left/right cards and
      values are visibly cut even though the document itself reports no
      horizontal overflow (`app/templates/training.html`, `.stat-grid`). Use one
      primary KPI plus two secondary KPIs, or a responsive 2+1 grid. Acceptance:
      all three labels and values fit at 320/390/430 px without clipping or
      horizontal scrolling.
- [x] **Settings → App Config card headers collapse into narrow columns on
      mobile.** `Training Schedule` / `Exercise Library` titles and their long
      descriptions share one horizontal `.card-header`. Stack title and helper
      copy vertically below 768 px, left-align both, and keep controls below the
      explanation. Acceptance: no word-by-word wrapping at 320/390 px.
- [x] **Blood Work hides the useful result columns on mobile.** The wide table
      leaves marker/unit/reference visible while dates, latest values and chart
      actions sit off-screen with no scroll affordance. Render a mobile result
      list/card with marker, latest value, status and change from previous;
      retain the full matrix as a desktop or explicit “All results” view.
- [x] **WOD confirmation movement picker is an unsearchable flat list.** When the
      LLM does not recognise a movement, assigning its equivalent means scanning
      one native `<select>` containing the entire exercise library. It has no
      section grouping, tags, search, recent choices or other context. The same
      flat options loop is duplicated for parsed, unmatched, seed and Alpine-
      added rows in `app/templates/wod_confirm.html`.
      Replace it with one shared movement-picker contract used by every row:
      search by movement name and tags; group results by section; show tags and
      metric in each result; put recently used movements first; support keyboard
      navigation; keep the unmatched LLM text visible while resolving it. The
      submitted value must remain the exact movement name expected by the current
      backend. Progressive enhancement is required: without Alpine/CDN JavaScript,
      render a usable native `<select>` grouped with `<optgroup>` rather than a
      dead custom widget. Acceptance: a movement can be found by partial name or
      tag in at most a few keystrokes, and the picker behaves identically for
      recognised rows, unmatched rows, manual seed rows and `+ dodaj ćwiczenie`.

#### P1 - make primary routes action-first

- [x] **Dashboard: redesign around “what matters today”.** Current mobile page is
      ~3413 px and gives Week, A.N.D.Y., six Oura KPIs, five measurements,
      experiments, Life Scores and a full-year calendar similar visual weight.
      Proposed order: Today hero (readiness + energy + one interpretation), one
      CTA (`Finish today's check-in`), compact A.N.D.Y. list, one weekly outcome,
      then Oura/measurements/Life Scores/calendar under `Insights` or progressive
      disclosure. The first viewport must contain orientation and the next action,
      not just the week strip.
- [x] **Goals: replace 8 areas × 3 always-open horizons with a focused map.** The
      empty state currently renders 24 identical `New goal...` inputs and is
      ~3356 px on mobile. Add a `Current focus` area (maximum three active goals),
      a horizon switch (`Now`, `1 year`, `3 years`, `10 years`), show populated
      areas by default, and use one clear `Add goal` flow that asks for area and
      horizon. Empty areas should be compact, not full-height cards.
- [x] **Experiment detail: remove duplicate logging paths.** Quick-log at the top
      and generic `Log Entry` below ask for the same data in different forms.
      Follow the Feniks flow: hero with one success criterion, one `Today`
      question generated from metric kinds, and `Different date or add details`
      to reveal the full form. Reduce the seven equal stats to outcome, target and
      time remaining. Move Complete/Abandon/Delete into secondary actions.
- [x] **Experiment progress language must match the calculation.** `0% elapsed`
      while the experiment is already in week 1/4 is technically derivable but
      reads as contradictory. Use real day-based elapsed progress or label the
      current semantics explicitly as `Week 1 of 4`; never call it elapsed time
      unless it measures elapsed time.

#### P2 - simplify secondary flows and interpretation

- [x] **Daily: make state changes explicit.** The circular tri-state toggle does
      not explain pending/done/skipped and a yellow minus is ambiguous. Provide
      explicit `Done` / `Skip` affordances (or visible state labels and
      `aria-pressed`), show a compact `n/7 complete today` summary, and render
      completed A.N.D.Y. tasks as content rather than permanent text inputs with
      edit-on-demand. Move Habit Streaks and Completion Heatmap under `Your
      trends`. Add semantic anchors to energy (`Low`, `OK`, `High`).
- [x] **Training: keep the one-note capture, polish its language and feedback.**
      Use one locale consistently, replace implementation copy `Zapisz i sparsuj`
      with user copy such as `Zapisz trening`, mark duration as optional, disable
      duplicate submit while parsing, and communicate that the raw note is saved
      before parsing. Show only new/recent PBs rather than a large grid of equal
      cards. Present history as a timeline with a useful session summary.
- [x] **Oura: add interpretation before charts.** Lead with readiness versus the
      7-day baseline and a short actionable explanation. Group equal KPI cards
      into Sleep, Activity and Recovery. Show one default trend with a metric
      selector; move the four-series dual-axis comparison behind `Compare
      metrics`. Reduce mobile axis labels and avoid squeezing four series into a
      small chart.
- [x] **Settings → Exercise Library: design it as a management tool.** Add search
      and section/tag filters; move `Add exercise` to a dedicated expandable panel
      or modal; render mobile entries as cards with a compact action menu instead
      of an internally scrolling edit table. Move technical paths such as the DB
      filename under `Advanced`.
- [x] **Progressive disclosure consistency.** Introduce shared patterns for
      `Today`, `Insights`, `History`, secondary forms and destructive actions so
      Dashboard/Daily/Training/Oura/Experiments do not each invent a different
      hierarchy. Keep the primary action in the first mobile viewport where the
      domain permits it.

**Pass 3 notes (2026-08-27):** Oura's interpretation is numbers and one
rule-derived word (readiness against its 7-day baseline, tolerance 3), not a
generated sentence - the same decision that kept the Dashboard hero free of a
verdict. The 4-series comparison chart was reordered under `Compare metrics`
rather than collapsed, because a Chart.js canvas inside a closed `<details>`
renders at zero size. One locale for the whole app stays OPEN below: it is an
app-wide decision, not a page fix, and it belongs with the i18n item.

#### Recommended delivery order

- [x] **UI/UX pass 1:** Training KPI responsiveness, App Config mobile headers,
      Blood Work mobile result cards, searchable/grouped WOD movement picker.
- [x] **UI/UX pass 2:** Today-first Dashboard, focused Goals map, single-flow
      Experiment detail.
- [x] **UI/UX pass 3:** Daily progressive disclosure, Oura interpretation, mobile
      Exercise Library and shared interaction patterns.

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
