# Security

## Reporting Security Issues

> Do not open issues that might have security implications!
> It is critical that security related issues are reported privately so we have time to address them before they become public knowledge.

Vulnerabilities can be reported by emailing:

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

Virgil is a **single-user, self-hosted** application. The threat model assumes:

- The application runs on a trusted network (home NAS, local machine) or behind a reverse proxy (Cloudflare Tunnel)
- A single authenticated user has full access to all data
- External attack surface is limited to the login page and Oura webhook endpoint

### Key Security Features

- **Authentication**: Email + password with bcrypt hashing, optional TOTP MFA
- **Session management**: Signed cookie sessions via `itsdangerous` (7-day expiry)
- **CSRF protection**: Double-submit cookie on all POST forms
- **Rate limiting**: Per-IP sliding window (120/min general, 10/min auth)
- **Encryption at rest**: Fernet encryption for OAuth tokens, LLM API keys, and webhook secrets
- **Security headers**: CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- **Webhook verification**: HMAC-SHA256 signature verification on Oura webhook payloads
- **Input validation**: Server-side validation with length limits on all text fields

## Preferred Languages

We prefer all communications to be in English.
