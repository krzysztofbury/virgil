# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
