# Escalation policy

Read by the `approver` role. A pull request that matches any item below goes
to the owner instead of merging on its own. The `retro` role proposes changes
to this file as pull requests; add what should have reached the owner, relax
what the owner keeps waving through, and give the reason in the PR.

## Always escalate

- Authentication, sessions, CSRF, TOTP, rate limiting, security headers, API
  tokens and scopes: `app/auth.py`, `app/csrf.py`, `app/rate_limit.py`,
  `app/security_headers.py`, `app/routers/api.py`, `app/mcp_endpoint.py`,
  `mcp_server/**`.
- Per-account data isolation: `app/user_db.py`, `app/central_db.py`, and any
  route that chooses which database to open.
- Database migrations, schema snapshot changes, and recovery or backup code:
  `app/migrations/**`, `app/central_migrations/**`, `app/recovery.py`,
  `tests/test_schema_snapshot.py`. Migrations are one-way.
- Server settings, mail, alert webhooks, off-site copies, AI keys, analytics
  and feature-flag allowlists: `app/server_settings.py`, `app/config.py`,
  `FEATURE_FLAGS` in `app/db.py`.
- Deploy, release, CI and hosting: `.github/**`, `infra/**`,
  `scripts/deploy.py`, `scripts/materialize_ansible_host.py`, `Dockerfile*`,
  `docker-compose*.yml`, `RELEASE.md`.
- Dependencies: `pyproject.toml`, `uv.lock`, `.github/dependabot.yml`.
- Legal and public copy about the service: `app/legal.py`, privacy, terms,
  pricing or limits, and `SECURITY.md`.
- Anything that needs a production run (deploy, Ansible, SSH, Settings >
  Server change); the PR merges only after the owner approves that run.

## May merge without the owner

When review passed, CI is green and nothing above applies:

- UI templates, styles and translations (`app/templates/**`, `app/static/**`,
  `app/locale/**`) with a browser check recorded in the PR.
- Features and fixes in routers and services outside the paths above, with
  tests.
- Tests and docs.
