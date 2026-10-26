# Security

## Reporting Security Issues

> Do not open issues that might have security implications!
> It is critical that security related issues are reported privately so we have time to address them before they become public knowledge.

Report a vulnerability privately through the repository's Security tab
(Report a vulnerability), or by emailing:

- virgil maintainer: [contact@datacraze.io](mailto:contact@datacraze.io)

Please include the requested information listed below (as much as you can provide) to help us better understand the nature and scope of the possible issue:

- Type of issue (e.g. credential exposure, injection, authentication bypass, XSS, CSRF bypass, etc.)
- Full paths of source file(s) related to the manifestation of the issue
- The location of the affected source code (tag/branch/commit or direct URL)
- Any special configuration required to reproduce the issue
- Environment (e.g. Linux / Windows / macOS, Docker / bare metal)
- Step-by-step instructions to reproduce the issue
- Proof-of-concept or exploit code (if possible)
- Impact of the issue, including how an attacker might exploit the issue

This information will help us triage your report more quickly.

## Security Model

Virgil is a **multi-user, self-hosted** application. The threat model assumes:

- The application runs on a trusted network (home NAS, local machine) or behind a reverse proxy (Cloudflare Tunnel)
- Each user's data lives in an isolated per-user SQLite database; the central registry holds only identities
- Registration is **closed by default** (`VIRGIL_REGISTRATION_OPEN=false`); the first account (bootstrap owner) can always be created
- External attack surface is limited to the login/signup pages, the health endpoint, and per-user HMAC-verified Oura webhook callbacks

### Key Security Features

- **Authentication**: Email first, then a password with bcrypt hashing (SHA-256 pre-hash) or a one-time emailed sign-in link (15 minutes, a confirming click, stored as a SHA-256). Optional TOTP MFA, required for password accounts when `VIRGIL_REQUIRE_TOTP` is on, with one-time recovery codes stored as hashes. The setup QR is built server-side for the signed-in user; the TOTP secret is never placed in a URL, and responses that carry it are not cached. An account without a password needs a sign-in from the last 15 minutes, or its second factor, to add a password, enroll an authenticator or create a token
- **API key binding**: `VIRGIL_API_KEY` serves only `VIRGIL_API_USER_EMAIL`. An empty email, or a missing or deactivated account, returns 503. The key does not follow another admin
- **Personal API tokens**: each account creates its own under Settings > Security. Only a SHA-256 of the secret is stored; a token serves its own active account with its own scopes (`read`, `write`, `sensitive`, `finance`), and a revoked or expired token, or one of a disabled account, gets 401 and never another account. Hosted `/mcp` tool calls carry the caller's credential to the REST API, never the server key
- **Session management**: Signed cookie sessions via `itsdangerous` (7-day expiry, HttpOnly, SameSite=Lax so OAuth callbacks work; all state changes are CSRF-protected). Each session carries the account's session epoch: logging out, turning MFA on or off, and disabling the account bump it and sign out every session
- **MFA attempt limit**: five wrong codes (authenticator or recovery) lock two-factor checks for that account for 15 minutes, at sign-in and in Settings alike, on top of the per-IP limit
- **CSRF protection**: Double-submit cookie on all POST forms (urlencoded and multipart). A write without the CSRF cookie is refused before its body is read, and the rate limit and auth both run before the body is buffered
- **Rate limiting**: Per-IP sliding window (120/min general, 10/min auth). Behind a proxy, `VIRGIL_TRUSTED_PROXY_IPS` takes addresses or CIDR ranges
- **Encryption at rest**: Fernet encryption for OAuth tokens, LLM API keys, and webhook secrets
- **Sensitive API scope**: Freedom data, including `/api/noporn` (intimate journal content), needs the `sensitive` scope: a token created with it, or `VIRGIL_API_SENSITIVE=true` for the server key
- **Finance API scope**: `/api/finance` (balances and the cushion) needs the `finance` scope, separate from the sensitive one: a token created with it, or `VIRGIL_API_FINANCE=true` for the server key
- **Security headers**: CSP (scripts from this origin or with a per-response nonce, never inline handlers or eval; every third-party asset is served locally), X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, on every response including errors, rate-limit refusals and login redirects
- **Privacy-safe PWA**: the service worker never caches authenticated HTML — only static assets and the public offline page
- **Webhook verification**: HMAC-SHA256 signature verification on Oura webhook payloads
- **Input validation**: Server-side validation with length limits on all text fields

## Repository Protections

The default branch requires a pull request, the `ci` and `image` checks and
signed commits, and refuses force pushes and deletion. Secret scanning with push
protection, Dependabot alerts and security updates, CodeQL default setup and
private vulnerability reporting are enabled. Workflows default to a read-only
token and need approval before running for outside contributors.

## Preferred Languages

We prefer all communications to be in English.
