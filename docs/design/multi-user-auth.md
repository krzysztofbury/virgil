# Accounts outside a trusted household

Proposal for [#56](https://github.com/krzysztofbury/virgil/issues/56). This is a design, not an implementation. It describes how one self-hosted Virgil instance can give invited people their own login, their own API and MCP credentials, and their own LLM key, while the owner's current Docker Compose setup keeps working.

Virgil stays a self-hosted app. Billing, cloud backups, and a public SaaS are [#58](https://github.com/krzysztofbury/virgil/issues/58) and are out of scope here.

## Decision

Support invited accounts on the existing instance. Keep registration closed unless the operator sets `VIRGIL_REGISTRATION_OPEN=true`.

Build, in order:

1. Session revocation and per-account MFA lockout ([#87](https://github.com/krzysztofbury/virgil/issues/87)), plus a logged-in password change.
2. Single-use invites and admin-issued password-reset codes. A short security-event log.
3. Per-user API tokens (hashed, scoped, revocable, optional expiry), accepted by `/api` and `/mcp` beside the existing server key.
4. A switch that stops invited accounts from spending the operator's `VIRGIL_INTERNAL_LLM_KEY`. Bring-your-own API keys already exist.

Defer MCP OAuth 2.1 until tokens exist and Cloudflare Access can let a browser finish the dance. Do not connect consumer subscriptions (Claude Pro/Max, ChatGPT Plus/Pro, Google AI Pro/Ultra, SuperGrok) via OAuth. The provider-by-provider verdicts are in [Per-user LLM credentials](#3-per-user-llm-credentials).

## What the code does today

Facts below match `main` after [#89](https://github.com/krzysztofbury/virgil/pull/89) (fail-closed API user, TOTP secret kept out of URLs).

### Two databases

The central registry (`VIRGIL_CENTRAL_DB_PATH`, `/data/virgil-central.db` in Compose) holds identities. Central migrations are contiguous files in `app/central_migrations/` with a `validate()` check. The latest is `002_provider_subscriptions`. `CENTRAL_SCHEMA_V1` is the immutable baseline for migration 001; new columns and tables land in a new migration, not by editing that string.

`users` today: `id` (UUID), unique `email`, bcrypt `password_hash`, `display_name`, `role` (`user` or `admin`), `db_filename`, `is_active`, Fernet `totp_secret`, `totp_enabled`, `created_at`, `last_login_at`.

Each account owns `data/users/{uuid}.db` (`USERS_DB_DIR` next to the central file). `create_user_db` runs the per-user migration chain (through `037`). Web sessions open that file in `AuthMiddleware`. The REST API opens it in `api_db`.

Oura webhook routing and subscription leases also live in the central database. Disable and delete refuse while a `provider_subscription_registrations` row exists, and `user_lifecycle_leases` stops a reset from racing a subscription change.

### Web login

`app/routers/auth.py` and `app/auth.py`:

- Signup is allowed when `VIRGIL_REGISTRATION_OPEN=true`, or when the registry is empty (bootstrap owner). The empty-registry insert is `WHERE (SELECT COUNT(*) FROM users) = 0`, so two concurrent first signups cannot both succeed.
- Passwords are SHA-256 pre-hashed, then bcrypt (12 rounds). Minimum length 8. Login verifies a dummy hash when the email is unknown.
- Optional TOTP. The secret is Fernet-encrypted. Setup QR is `GET /mfa/qr.png` with no query string ([#86](https://github.com/krzysztofbury/virgil/issues/86), fixed). Login with MFA sets a 10-minute cookie whose payload is `_mfa_pending:{user_id}`. `/mfa/verify` has no per-account failure counter ([#87](https://github.com/krzysztofbury/virgil/issues/87)).
- A full session is an `itsdangerous.TimestampSigner` token of the user UUID, 7 days, `HttpOnly`, `SameSite=Lax`. The signing key is SHA-256 of the Fernet key. Logout clears the cookie and leaves the token valid until it expires ([#87](https://github.com/krzysztofbury/virgil/issues/87)). There is no session table and no password-change form.
- Middleware rejects a session whose user is missing or `is_active = 0`, and drops the cookie. A disabled account therefore cannot use the web UI. Re-enabling the account makes the old cookie work again, because nothing on the server was revoked.

`VIRGIL_ADMIN_EMAILS` is a comma-separated list. `create_user` sets `role=admin` when the email is in the list. `promote_admin_emails()` runs at startup and promotes matching rows. It never demotes. The admin page (`/admin/users`) can disable, enable, and delete. It displays the role and does not change it. Delete is hidden in the template for emails in `VIRGIL_ADMIN_EMAILS`; disable is not. There is no invite row and no reset-code row.

### API and MCP

`VIRGIL_API_KEY` is one server-wide secret, compared with `hmac.compare_digest` in `require_api_key`. Empty key: `/api` returns 403. Set key with empty `VIRGIL_API_USER_EMAIL`: startup logs an error and every `/api` request returns 503. Set key whose email is missing or deactivated: 503. There is no fallback to "first active admin" ([#83](https://github.com/krzysztofbury/virgil/issues/83), fixed by #89).

`/mcp` (`app/mcp_endpoint.py`) calls the same `require_api_key`, then the MCP SDK in stateless JSON mode. Tool handlers still HTTP-call the process's own REST API. The client is process-wide:

```python
virgil_mcp.use_client(
    httpx.Client(base_url=loopback_base_url(HOST, PORT), headers={"X-API-Key": API_KEY}, timeout=30.0)
)
```

Every hosted tool call re-sends the server key, not the caller's header. The stdio proxy (`mcp_server/virgil_mcp.py`) sends `X-API-Key: $VIRGIL_API_KEY` plus optional `CF-Access-Client-Id` and `CF-Access-Client-Secret`.

`VIRGIL_API_SENSITIVE` (default false) is also server-wide. It gates `/api/noporn`, `/api/freedom` and its writes, and the Freedom streak inside `/api/summary`.

Compose puts Virgil behind `cloudflared` (`tunnel run`, no published ports) with Watchtower updating the `virgil` container. Cloudflare Access is a separate gate in front of that hostname. The service token authenticates the MCP client machine. It does not identify a Virgil user.

### LLM keys

Per-user `llm_providers` stores `provider`, Fernet `api_key_enc`, LiteLLM `model`, and `is_active`. Settings can add, activate, and delete a row. `call_llm` uses the active row, then falls back to `VIRGIL_INTERNAL_LLM_MODEL` / `VIRGIL_INTERNAL_LLM_KEY`.

Two callers skip the user row and always use the env key: onboarding enrichment (`app/services/onboarding.py`, `_llm_call`) and medical PDF/text extraction (`app/services/medical_import.py`). An invited user with no key of their own still spends the operator's key on every `call_llm` path, and cannot make onboarding or blood-work import use their own key.

The Fernet key is `VIRGIL_ENCRYPTION_KEY` or `/data/virgil.key`. One key decrypts every user's LLM key, Oura tokens, and TOTP secret. That is acceptable on a machine the operator controls, and invited people need to know the operator can read those secrets.

### Rate limit and request body

`app/rate_limit.py` buckets by peer IP. With the tunnel as the only ingress and `VIRGIL_TRUST_CLOUDFLARE_HEADERS=false`, every client shares cloudflared's address ([#84](https://github.com/krzysztofbury/virgil/issues/84)). Ten login attempts a minute from anyone then blocks login for everyone.

CSRF reads the body (up to 21 MB for multipart) before auth and rate limiting ([#85](https://github.com/krzysztofbury/virgil/issues/85)).

## 1. Accounts, roles, and lifecycle

### Options

**A. Leave registration closed and add invites.** An admin copies a single-use link. The first account still bootstraps with no invite. `VIRGIL_REGISTRATION_OPEN` remains an explicit override.

**B. Turn registration on.** Anyone who can reach the hostname can create an account and a SQLite file. The config comment already calls out disk and LLM cost. Combined with #84, one client can also lock login for the house.

**C. External identity (Authentik, Cloudflare Access JWT as the Virgil user).** Access already sits in front of the hostname, but the app does not read `Cf-Access-Jwt-Assertion`. This would replace email/password for the web UI and leave MCP, TOTP, and password reset to redesign. It also ties every deploy to Access.

**Recommendation: A.** Invites match a self-hosted box with a handful of people. B stays available and off. C is a different product; do not start it here.

### Roles

Keep the `user` / `admin` check constraint. Do not add a third role.

`VIRGIL_ADMIN_EMAILS` stays the break-glass list:

- Startup promotion stays. An email on the list is an admin after restart even if a row was demoted.
- The admin UI may promote or demote other accounts. It must refuse to demote, disable, or delete an email that is currently on the list. The delete button already hides for those emails; disable does not, and nothing stops a second admin from disabling the owner.
- Removing an email from the env var does not demote the row. Demotion is an explicit UI action after the next restart. Automatic demotion on a typo in `.env` would lock the operator out.

`VIRGIL_API_USER_EMAIL` is only the account the legacy server key may touch. It does not grant admin, and it is not "the API user" once per-user tokens exist. Leave the variable in place for as long as `VIRGIL_API_KEY` is set. Unset the pair together, after a personal token works. Do not repurpose the variable as a default token owner.

### Password change and reset

There is no mailer. Do not add SMTP in the first cut.

- **Logged-in change** (Settings): current password, new password, confirmation. On success, set `password_hash` and bump the session epoch (below). The browser that submitted the form gets a fresh cookie.
- **Admin-issued reset:** the admin page creates a single-use code for that user, shows the link once, and stores only SHA-256 of the secret. TTL 15 minutes. The public form sets a new password and bumps the epoch. A second use fails. Add `/reset` to the auth rate-limit set (`AUTH_PATHS` already includes `/login`, `/signup`, and `/mfa/verify`).
- **Later:** if an SMTP setting exists, the same row can be emailed. The hash, TTL, and single use stay.

The reset link will contain a secret, so it can land in Cloudflare or browser history. That is the #86 failure mode. Constraints that make it acceptable: high entropy (`secrets.token_urlsafe(32)`), 15-minute TTL, single use, hash at rest, `Cache-Control: no-store`, and the existing `Referrer-Policy`. Do not put TOTP secrets or API tokens in URLs. Do not log the reset query string at info level.

### Session revocation ([#87](https://github.com/krzysztofbury/virgil/issues/87))

Add `users.session_epoch INTEGER NOT NULL DEFAULT 0`.

Sign `v2:{user_id}:{epoch}` instead of the bare UUID. A pending MFA login is `v2:_mfa_pending:{user_id}:{epoch}` and keeps the 10-minute max age. Middleware accepts a legacy bare-UUID token only while that user's epoch is still 0, so current cookies survive the deploy. The first bump invalidates them.

Bump the epoch on logout, password change, reset completion, MFA enable, MFA disable, and admin disable. Logout then signs out every device. That matches #87's done-when. A per-device session list (id, user agent, last seen, revoke one) can follow; it needs a `sessions` table and is not required to close #87.

Also add `mfa_failed_count` and `mfa_locked_until`. After 5 bad codes, refuse verification until 15 minutes after the fifth failure. Count by user id, not by IP, because #84 makes the IP bucket one shared counter. A successful code clears the counter. A locked account still returns the generic login error on the password step so the response does not become a user-enumeration oracle beyond what timing already resists.

Admin disable already blocks the web UI via `is_active`. The epoch bump covers the case where the account is enabled again and an old cookie would otherwise resume. API tokens are covered in section 2: a deactivated user cannot authenticate with a token.

### Invites

`invites` in the central database:

| Column | Notes |
|---|---|
| `id` | UUID |
| `token_hash` | SHA-256, unique |
| `email` | Optional. When set, signup must use that address |
| `role` | `user` or `admin`, default `user` |
| `allow_shared_llm` | Default 0. See section 3 |
| `created_by` | Admin user id |
| `expires_at` | Default 7 days |
| `used_at`, `used_by` | Set in the same transaction as the user insert |

Signup with a closed registry and at least one user requires a valid invite. The bootstrap insert is unchanged. Consuming the invite and inserting the user happen in one transaction. An invite email that already has an account is an error and does not consume the invite.

The admin copies the link. No email is sent.

### Account lifecycle

Keep the current disable/delete guards (subscription registration must be gone, lifecycle lease must be free). Extend them:

- Disable bumps `session_epoch`. Token lookup requires `is_active = 1`, so API and MCP stop immediately. Re-enable restores the tokens; the admin can revoke them first if that is wrong.
- Delete already removes the central row and the SQLite file (and `-wal` / `-shm`). Also delete unused invites, reset codes, and tokens (foreign key `ON DELETE CASCADE`). [#65](https://github.com/krzysztofbury/virgil/issues/65) still applies: backup files for that database need a deletion path. [#67](https://github.com/krzysztofbury/virgil/issues/67) still applies: a missing user database must not be recreated as an empty file on the next request.
- Factory reset (`POST /settings/factory-reset`) currently needs a session and no password. Require the current password once accounts are not all the operator. It already takes a lifecycle lease and refuses while Oura subscriptions are live.
- Self-service export (CSV) stays. Self-service account deletion can wait; an admin can delete.

### Audit

#56 asks for an access audit. Record security events, not health data.

`auth_events` in the central database: `id`, `created_at`, `user_id` (nullable), `event` (`login_ok`, `login_fail`, `mfa_fail`, `mfa_lock`, `logout`, `password_change`, `password_reset`, `invite_create`, `invite_consume`, `token_create`, `token_revoke`, `user_disable`, `user_delete`), `ip` (the same identity the rate limiter uses), `detail` (short, no secrets, no journal text).

Do not write a row per API read. Token use updates `last_used_at` (section 2). Cap the table (delete rows older than 180 days from the existing scheduler) so a public login form cannot fill the disk.

### Schema (central migration 003, then 004)

Migration 003, with the #87 work:

```sql
ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN mfa_failed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN mfa_locked_until TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN allow_shared_llm INTEGER NOT NULL DEFAULT 1;

CREATE TABLE auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    user_id TEXT,
    event TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
```

`allow_shared_llm` defaults to 1 so every existing account, including the owner, keeps today's fallback. `create_user` must set the column explicitly. The SQL default is 1, and an insert that omits it would hand a public signup the operator's key. Bootstrap owner: 1. Invite: the invite's value (default 0). `VIRGIL_REGISTRATION_OPEN` signup: 0.

Migration 004, with invites and resets:

```sql
CREATE TABLE invites (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
    allow_shared_llm INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL REFERENCES users(id),
    expires_at TEXT NOT NULL,
    used_at TEXT NOT NULL DEFAULT '',
    used_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE password_resets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT NOT NULL DEFAULT ''
);
```

Follow the existing central-migration style: one `up()`, and `validate()` checking `PRAGMA table_info` and foreign keys. Do not edit `CENTRAL_SCHEMA_V1`.

### Rollout

| Step | Size | Ships |
|---|---|---|
| 0a | Small | #87 epoch, MFA lockout, logged-in password change. No new public routes |
| 0b | Small, separate issues | #84 (CIDR or a stable cloudflared identity, and a startup warning when `cf-connecting-ip` arrives while trust is off) and #85 (refuse a cookieless request before reading the body). Do this before handing invites to anyone outside the house |
| 1 | Medium | Invites, admin reset codes, promote/demote, `auth_events`, `allow_shared_llm` honored (section 3) |

Phase 1 does not change `VIRGIL_API_KEY` or `VIRGIL_API_USER_EMAIL`.

## 2. Per-user API and MCP access

### Options

**A. Per-user tokens in Settings, legacy env key kept.** Lookup by hash. The stdio proxy and `claude mcp add --header` keep sending `X-API-Key`. The owner swaps the secret when ready.

**B. MCP OAuth 2.1 first**, so Claude and ChatGPT connect with a browser login and no pasted key.

**C. One extra env key per user** (`VIRGIL_API_KEYS=email:secret,...`). No UI, no scopes, no revoke without a restart, and the secrets sit in `.env` next to the operator's key.

**Recommendation: A now, B later.** C repeats the single-key problem in a longer string. B is what hosted MCP clients want, and it is a large authorization server that Cloudflare Access currently blocks. Tokens unblock every client the owner uses today.

### Token model

Central table, migration 005. Tokens cannot live in the per-user SQLite file: authentication happens before that file is opened, and a hash lookup cannot scan every file.

```sql
CREATE TABLE api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    scopes TEXT NOT NULL,
    expires_at TEXT NOT NULL DEFAULT '',
    last_used_at TEXT NOT NULL DEFAULT '',
    revoked_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX api_tokens_user ON api_tokens(user_id);
```

- Secret: `vrg_` + `secrets.token_urlsafe(32)`, shown once in the HTML body, never stored.
- `token_hash`: SHA-256 of the full secret. These are high-entropy strings, so bcrypt is unnecessary and would forbid the unique index.
- `token_prefix`: the first 12 characters, so the Settings list can show `vrg_abc12345…` and the name.
- `scopes`: space-separated subset of `read`, `write`, `sensitive`.
- `expires_at`: empty means no expiry. The form defaults to 90 days and offers "no expiry" so a long-lived Claude Code token is a conscious choice.
- Revoke sets `revoked_at`. The next request fails. Do not cache lookups; SQLite is enough, and a cache would make revoke lag.

### How a request authenticates

`require_api_key` grows into "resolve a credential". On every request that presents a secret, both compare the env key and hash the secret, so timing does not reveal which store matched.

1. Read `X-API-Key`, else `Authorization: Bearer`. If both are present they must match. A missing secret is 401, except the case in step 2.
2. If `VIRGIL_API_KEY` is empty and no `api_tokens` row is live (unrevoked, unexpired), return 403 `API disabled`, same as today. A fresh install does not look like a wrong password.
3. If `VIRGIL_API_KEY` is set and `hmac.compare_digest` matches, keep today's behavior: `VIRGIL_API_USER_EMAIL` must be set and must name an active user, otherwise 503. Scopes are read+write, plus `sensitive` only when `VIRGIL_API_SENSITIVE=true`. The startup warning from #89 stays. This path does not consult `api_tokens`.
4. Otherwise load the row whose `token_hash` is SHA-256 of the secret, `revoked_at` is empty, `expires_at` is empty or in the future, and the user is active. A hit binds that `user_id`, `db_filename`, and the token's scopes. Anything else is 401. A deactivated user is 401, not a switch to another account.

Scope checks, after the user database is open:

| Scope | Allows |
|---|---|
| `read` | GET routes other than Freedom and `/api/noporn` |
| `write` | POST, PUT, PATCH, DELETE other than Freedom |
| `sensitive` | `/api/noporn`, `/api/freedom` and its writes, and the Freedom object inside `/api/summary` |

Missing scope: 403 with a detail that names the scope. `/api/summary` stays 200 for a read token and returns `feniks: null` without `sensitive`, which is what `VIRGIL_API_SENSITIVE=false` does today.

The legacy key does not consult `api_tokens`. Creating a personal token does not disable it.

### Hosted `/mcp` and the loopback client

The process-wide `httpx` client must stop stamping `API_KEY` on every tool call. Set a `contextvar` to the presented secret inside `MCPEndpoint.__call__` before `handle_request`, and have `virgil_mcp._headers()` read it. Reset the contextvar when the request finishes. Stateless mode has no MCP session to bind a user to, so the credential has to ride on each HTTP request. That is already how `X-API-Key` works.

The stdio proxy needs no protocol change. The owner puts the personal token in the proxy's `VIRGIL_API_KEY`. Cloudflare Access service-token headers stay as they are: Access names the client machine, the Virgil token names the user.

### Migration that keeps the current setup working

Do not copy `VIRGIL_API_KEY` into `api_tokens` on startup. The operator would then have two live copies of a secret they rotate in `.env`.

Order for the owner's Compose deploy:

1. Deploy token support. Existing proxy and `claude mcp add --header` calls keep using the env key. Behavior matches #89.
2. Sign in as the account named by `VIRGIL_API_USER_EMAIL`. Create a token with the scopes that key uses today (include `sensitive` only if `VIRGIL_API_SENSITIVE=true`).
3. Put that token in the proxy env and in any stored MCP header. Confirm a tool call.
4. When ready, remove `VIRGIL_API_KEY` and `VIRGIL_API_USER_EMAIL` from the server env and restart. Personal tokens keep working. The old secret stops working at restart, which is the revocation.

Until step 4, both secrets work and both map to a single explicit account. Nothing in this sequence reintroduces the "first active admin" fallback.

### MCP OAuth 2.1 — later

The MCP authorization spec (revision 2025-11-25, and the 2026-07-28 discovery rules) says a remote MCP server that wants browser-based clients must:

- Answer `401` with `WWW-Authenticate` carrying a `resource_metadata` URL ([RFC 9728](https://www.rfc-editor.org/rfc/rfc9728)).
- Serve protected-resource metadata whose `authorization_servers` lists an authorization server. Clients also fetch `/.well-known/oauth-protected-resource` and the path-scoped variant under `/mcp`.
- Run an OAuth 2.1 authorization server: authorization-code with PKCE `S256`, refresh tokens, and authorization-server metadata ([RFC 8414](https://www.rfc-editor.org/rfc/rfc8414)). Clients must refuse a server that does not advertise PKCE.
- Prefer [OAuth Client ID Metadata Documents](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) for registration. Dynamic client registration (RFC 7591) is optional and, in the current draft, backwards compatibility.

That is an authorization server inside Virgil: consent screen, redirect allow-list, short-lived access tokens, refresh-token rotation, and metadata routes that are public. Issued access tokens should be rows in `api_tokens` (or a sibling table the same resolver reads) so revoke, expiry, and scopes are one mechanism. Audience is this server's `/mcp` URL only.

Why it waits:

- Claude Code on the owner's machine already connects through the stdio proxy with a header and a Cloudflare Access service token. A personal Virgil token drops into that header.
- ChatGPT and Claude's hosted connectors start the OAuth dance from their cloud, without the Access service token. The compose tunnel is the only ingress, and Access challenges unauthenticated browsers. The well-known URLs, the authorize redirect, and the token endpoint would all have to be reachable. Opening them means an Access bypass (or a second hostname) in front of new unauthenticated routes. Those routes must be in the #85 "do not buffer the body first" path and in a rate-limit bucket that is not the whole tunnel (#84).
- Building the authorization server before tokens exist would invent a second credential store.

Recommendation: **later**. Revisit when a hosted connector is actually required. First decision at that point is the Access bypass list, not the OAuth library.

### Rollout

| Step | Size | Ships |
|---|---|---|
| 2 | Medium | Migration 005, Settings create/list/revoke, resolver in `api_db` and `/mcp`, contextvar so loopback calls forward the caller secret, tests that a token cannot see another user's database and cannot see Freedom without `sensitive` |
| 3 | Large | OAuth 2.1 authorization server, only after step 2 and an Access decision |

## 3. Per-user LLM credentials

### What to build

Bring-your-own API keys are already the data model: `llm_providers.api_key_enc` is Fernet ciphertext, and Settings writes it. The gap is who pays when the user has no row, and which call sites ignore the row.

**Recommendation:**

- Keep storing provider API keys in `llm_providers`, encrypted with the server Fernet key. Document on the Settings page that the instance operator can decrypt them.
- Honor `users.allow_shared_llm`. `call_llm`'s resolver uses the active user row first. It uses `VIRGIL_INTERNAL_LLM_*` only when the flag is 1. Existing rows default to 1, so the owner's server key still covers the owner. Invites default the flag to 0. `VIRGIL_REGISTRATION_OPEN` signups default to 0. The bootstrap owner gets 1.
- An admin can flip the flag per user. The admin list should show it. Turning it off does not delete the env key.
- Send onboarding enrichment and medical import through the same resolver. Onboarding already skips enrichment when no internal key is set; that skip must also happen when the user has no row and the flag is 0. Medical import needs a multimodal call on the resolved key (the PDF path sends a document part). It must not keep a private path that only reads `INTERNAL_LLM_KEY`.
- Scheduler jobs already call `call_llm` per user database. Once the resolver checks the flag, a background briefing cannot spend the operator's key for an opted-out user.

No new table. The column is in migration 003 so it exists before invites are issued.

### Consumer-subscription OAuth

Question: can a user connect Claude Pro/Max, ChatGPT Plus/Pro, a Google AI subscription, or SuperGrok, and have Virgil's server-side features (onboarding, A.N.D.Y., experiment summaries, WOD parsing, briefings, medical import) bill that subscription instead of an API key?

Virgil would have to store a refresh token in the per-user database and call the provider from the container, including from the scheduler. That shape is what the verdicts below are about. Sources were read on 18 October 2026.

#### Anthropic (Claude Free / Pro / Max) — no

**Supported OAuth for a third-party app:** no.

Anthropic's Claude Code legal page says OAuth is for Claude subscription plans used in Claude Code and other native Anthropic applications. Third-party developers "should use API key authentication through Claude Console or a supported cloud provider." Anthropic "does not permit third-party developers to offer Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials on behalf of their users." Developers "may not collect, store, or intermediate Claude.ai credentials or session tokens." The same page says this does not restrict customers managing their own API keys, which is the path Virgil already has.

Source: [Claude Code, Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance), "Authentication and credential use", read 18 October 2026. The February 2026 clarification of the same rule is reported in [The Register, 20 February 2026](https://www.theregister.com/software/2026/02/20/anthropic-clarifies-ban-on-third-party-tool-access-to-claude/5014546). A feature request to allow Pro/Max OAuth in third-party apps was still open with no product change: [anthropics/claude-code#82266](https://github.com/anthropics/claude-code/issues/82266).

**What a hypothetical token would grant:** nothing Virgil is allowed to hold. The documented third-party credential is an API key (`sk-ant-api…`) billed on the API account, or Workload Identity Federation / App Attest for API workloads ([Claude API authentication](https://platform.claude.com/docs/en/manage-claude/authentication)).

**Verdict: no.** Store an Anthropic API key when the user pastes one. Do not implement Claude.ai or `claude setup-token` login.

#### OpenAI (ChatGPT Go / Plus / Pro) — no for this server

**Supported OAuth for a third-party app:** yes, as Sign in with ChatGPT, with a hard constraint Virgil's architecture does not meet.

The product can grant two different things:

- Identity (`openid profile email`): name, email, profile picture. It does not include conversations or API access.
- ChatGPT plan usage, only if the user turns it on: eligible Responses API requests count against the plan's included ChatGPT Work and Codex usage, or against credits the user explicitly allowed. It does not reveal the user's OpenAI API key or ChatGPT conversations. Go, Plus, and Pro can do this on participating commercial apps. Open-source tools are described separately.

Sources:

- [Sign in with ChatGPT quickstart](https://developers.openai.com/siwc/quickstart), read 18 October 2026. Commercial plan usage is "selected commercial partners through a limited trial." Plan usage is "available to all open-source partners and selected private clients."
- [Sign in with ChatGPT Terms](https://openai.com/policies/sign-in-with-chatgpt-terms/), 29 September 2026. Persistent storage of access and refresh tokens "must be local and under the user's control, not in a remote or managed environment." Requests "must originate from the user's local runtime or a remote runtime only that user controls." The app must not let one user's activity trigger requests on another user's account, must not offer the plan as a general-purpose API for other tools, and must not charge the user to use their plan through this integration.
- [Cookbook: Integrating Sign in with ChatGPT in your open-source app](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt), read 18 October 2026. "The ChatGPT plan usage integration described here is available for open-source tools and personal projects that run locally. If you're building a paid or remotely hosted app, join the waitlist."
- [Help Center, Using your ChatGPT plan in other apps and sites](https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites). The page said "Updated: 11 days ago" on 18 October 2026, so about 7 October 2026. It describes participating apps, not a self-serve credential for an arbitrary server.

**Why Virgil should not implement it.** LLM calls run in the container. Briefings and other jobs run from the scheduler when the user is not at the keyboard. The token would be Fernet-encrypted in `data/users/*.db` on the NAS, which is remote storage, not local storage under the user's control. A household NAS the operator personally controls is still the environment the terms exclude. Several accounts on one process also make it easy to violate "another user's activity must not trigger requests." Scraping a Codex CLI session token would be outside this supported flow entirely.

**Verdict: no** for the Virgil server. **Later** only if OpenAI publishes a path whose terms allow a remotely hosted, multi-user app to store refresh tokens and run background calls, and Virgil is accepted onto it. A local-only sidecar that keeps the token on the laptop and never sends it to Virgil is a different product; it cannot drive server-side jobs, so it is not a substitute. API keys in `llm_providers` remain the supported OpenAI path. The terms say those restrictions do not affect separately authorized API use.

#### Google (Gemini app, Google AI Pro / Ultra, AI Studio) — no

**Supported OAuth that bills a consumer Google AI subscription from a third-party app:** no.

[Google AI plans](https://ai.google.dev/gemini-api/docs/google-ai-plans), last updated 16 October 2026: Pro and Ultra quotas apply to the AI Studio Playground and Build UI. "AI Studio UI only: Google AI plan benefits for developer usage apply only within the Google AI Studio web interface. Direct use of the Gemini API (such as using API keys or external applications) is billed and managed separately."

Gemini CLI's own docs forbid reusing its Google-login token:

- [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/), "Why can't I use third-party software like Claude Code, OpenClaw, or OpenCode with Gemini CLI?". "Using third-party software, tools, or services to harvest or piggyback on Gemini CLI's OAuth authentication to access our backend services is a direct violation of our applicable terms and policies" and "may be grounds for immediate suspension or termination of your account." The supported method named there is a Vertex AI or Google AI Studio API key. The page's "Last updated" line says 10 April 2026; the same page also notes that unpaid and Google One Gemini CLI use was replaced by Antigravity CLI on 18 June 2026. The prohibition was still on the page on 18 October 2026.
- [Gemini CLI terms and privacy](https://geminicli.com/docs/resources/tos-privacy/): "Directly accessing the services powering Gemini CLI (for example, the Gemini Code Assist service) using third-party software, tools, or services (for example, using OpenClaw with Gemini CLI OAuth) is a violation of applicable terms and policies."

**What a Google OAuth token actually is here:** either identity ("Sign in with Google", no model quota) or a first-party Gemini Code Assist / Gemini CLI token. Neither is a grant for Virgil.

**Verdict: no.** A user-pasted Gemini API key or Vertex key, stored in `llm_providers`, is the supported path. The operator's current `VIRGIL_INTERNAL_LLM_KEY` is that kind of key. Do not proxy `~/.gemini` OAuth tokens.

#### xAI (Grok, SuperGrok, X Premium) — no

**Supported general OAuth for an arbitrary third-party app:** no.

The documented developer credential is an API key from the xAI console, sent as `Authorization: Bearer` to `https://api.x.ai`. [Accounts FAQ](https://docs.x.ai/developers/faq/accounts): a Grok account can be the same login as the API account, and "the billing is separate for Grok and xAI API." [REST API reference](https://docs.x.ai/developers/rest-api-reference/inference), last updated 8 October 2026, authenticates with a console API key.

xAI has published OAuth for specific coding agents, not for a personal-tracking server:

- [Use Grok in OpenCode](https://x.ai/news/grok-opencode), read 18 October 2026: SuperGrok or X Premium inside OpenCode, to code with Grok Build. "More open-source agents and integrations are coming soon."
- [Connect Grok to Hermes Agent](https://x.ai/news/grok-hermes) is the same kind of named integration.

Those pages are an allowlist of agents, not a registration program Virgil can join. Copying the device-code flow those agents use would be an unofficial client against a surface xAI has not offered to arbitrary apps. Consumer terms also say the user "may not share your account credentials or make your account available to anyone else" ([xAI consumer terms](https://x.ai/legal/terms-of-service), read 18 October 2026). An OAuth token minted for OpenCode is not a credential Virgil is a party to.

A third-party bug report, not an xAI document, says the Hermes OAuth path returned HTTP 403 for a standard SuperGrok subscription while the dashboard described Grok Build as SuperGrok Heavy only ([NousResearch/hermes-agent#26847](https://github.com/NousResearch/hermes-agent/issues/26847)). Treat that as evidence the surface is gated and unstable, not as a protocol to implement.

**Verdict: no.** Store an xAI API key from console.x.ai. Revisit only if xAI documents an OAuth client for self-hosted apps whose tokens are allowed to call ordinary chat completions on a SuperGrok quota, with a published rule for where the refresh token may be stored.

### Summary

| Provider | Subscription OAuth for Virgil | What to accept instead |
|---|---|---|
| Anthropic Claude Pro/Max | **No.** Explicitly prohibited | User's Anthropic API key |
| OpenAI ChatGPT Plus/Pro/Go | **No** for this server. The supported product forbids remote token storage and is limited to local open-source tools or approved partners | User's OpenAI API key. Revisit only if OpenAI approves a hosted multi-user app |
| Google AI Pro/Ultra, Gemini | **No.** Subscription benefits are AI Studio UI-only; CLI OAuth reuse is a stated terms violation | Gemini API key or Vertex key |
| xAI SuperGrok / X Premium | **No.** Official OAuth is for named coding agents; API billing is separate from Grok | xAI console API key |

## Security issues already open

| Issue | Relation to this proposal |
|---|---|
| [#83](https://github.com/krzysztofbury/virgil/issues/83) | Closed by #89. The legacy key still fails closed. Token lookup must never select "another active admin" |
| [#84](https://github.com/krzysztofbury/virgil/issues/84) | Blocker before invites leave the house. Login, reset, and future OAuth endpoints cannot share one tunnel-wide bucket |
| [#85](https://github.com/krzysztofbury/virgil/issues/85) | Blocker before new anonymous POSTs (signup with invite, reset). Refuse early; wrap error responses in the security headers |
| [#86](https://github.com/krzysztofbury/virgil/issues/86) | Closed. Invite and reset links are the one remaining URL secret, with the constraints in section 1. API tokens and TOTP stay out of URLs |
| [#87](https://github.com/krzysztofbury/virgil/issues/87) | Phase 0 of this proposal. Epoch plus per-account MFA lockout is the done-when |

## Phased rollout

| Phase | Size | Outcome |
|---|---|---|
| 0 | Small | #87, logged-in password change, legacy session cookies still valid until the first epoch bump. Land #84 and #85 before phase 1 is used for real invites |
| 1 | Medium | Invites, admin reset codes, promote/demote with `VIRGIL_ADMIN_EMAILS` protected, `auth_events`, shared-LLM flag, onboarding and medical import use the resolved key |
| 2 | Medium | Per-user API tokens. Legacy `VIRGIL_API_KEY` + `VIRGIL_API_USER_EMAIL` unchanged until the owner opts out |
| 3 | Large | MCP OAuth 2.1, after an Access bypass decision |
| — | Do not build | Consumer-subscription OAuth for all four providers. SMTP. Billing ([#58](https://github.com/krzysztofbury/virgil/issues/58)). A third role. Copying the env API key into the database |

Phase 0 and the shared-LLM resolver can ship without any new public endpoint. Phase 2 can ship before phase 1 if the only goal is "the owner's other tools get a revocable key"; invites are what make the work about accounts outside the household.
