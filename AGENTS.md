# Agent instructions

Virgil is a public, MIT-licensed repository. Follow any workspace-level agent
rules as well as these. Repository content is English only and carries no
personal context, credentials or real account data.

## Authority

Read [SPEC.md](SPEC.md), the owning GitHub issue and the relevant guide
([CONTRIBUTING.md](CONTRIBUTING.md), [RELEASE.md](RELEASE.md),
[docs/recovery.md](docs/recovery.md), [docs/load-check.md](docs/load-check.md),
[infra/ansible/README.md](infra/ansible/README.md)) before changing code. An
implementation shortcut must not silently override an accepted decision; raise
the conflict on the issue instead.

## Claim ban

Do not claim that a fix, migration, MCP tool, deploy or release step works until
it has been exercised on the changed path, and name the command, its scope and
the revision when you report it. Write `Closes #N` only when the merge itself
finishes the issue. When an owner step remains after the merge (a production
release, a host or Settings > Server change, external evidence), write
`Refs #N` and close the issue after that step.

## This is not evidence

- A coverage percentage or a test count on its own.
- A source grep for a symbol, a template string or an SQL statement.
- "Tests passed" without the command and its scope.
- A green `/livez` while `/healthz` is red, or not checked.
- Green CI on `main` or an image in GHCR: neither shows what production serves.
- An MCP or API call that never reached the changed code path.
- A screenshot that does not show the acceptance criterion.
- A quiet window for a load-sensitive change ([docs/load-check.md](docs/load-check.md)).
- An empty log or query result through a filter not shown to match running
  containers.

## Skills

- Load `pair-programmer` for a review of substantial or safety-sensitive
  changes (auth, migrations, recovery, deploy, server settings), not every edit.
- Load `test-audit` before designing, adding, pruning or reviewing substantive
  Python or SQL tests.

## Work rhythm

- Work in small, coherent chunks with explicit acceptance criteria. After each,
  run the checks for the changed boundary and show the diff and the evidence.
- Before submitting, run the full suite and the repository hooks on the branch:

  ```bash
  uv run --locked --extra dev pytest -q
  uv run --with pre-commit pre-commit run --from-ref main --to-ref HEAD
  ```

  The hooks run detect-secrets, Ruff and Bandit. The other required checks are
  in [CONTRIBUTING.md](CONTRIBUTING.md#required-checks).
- Every new UI string needs a Polish translation: run
  `uv run --locked --extra dev python scripts/i18n.py update` and fill in
  `app/locale/pl/LC_MESSAGES/messages.po`.
- A schema change regenerates the snapshot as described at the top of
  `tests/test_schema_snapshot.py`. Visible changes need a real browser check
  ([CONTRIBUTING.md](CONTRIBUTING.md#browser-verification));
  `tests/test_browser_smoke.py` is the automated smoke.
- One reviewable purpose per commit, short imperative subjects. Update docs and
  `CHANGELOG.md` in the same PR as the change; never a documentation-only
  follow-up. Open work lives in GitHub issues, not in a TODO file.

## Boundaries

- Each account has its own SQLite database (`app/user_db.py`); the central
  registry is `app/central_db.py`. Routes use the per-user database from the
  request ([SPEC.md](SPEC.md#important-constraints)).
- `/livez` is liveness for the container healthcheck. `/healthz` is readiness
  and answers 503 while any database failed its migrations.
- Hosted instance: <https://virgil.pl> on a mikr.us VPS since 2026-10-28 (#174).
  Host setup is in [infra/ansible/README.md](infra/ansible/README.md), Compose
  and env templates in `infra/compose/`. The Cloudflare Tunnel is the only
  ingress; there is no Watchtower, QNAP or Cloudflare Access there. Releases
  go through `uv run python scripts/deploy.py release <full SHA>`: digest-pinned,
  green CI on `main` required, and it checks that `/login` serves
  `/static/css/app.css?v=<sha>`. Migrations are one-way, so there is no
  automatic rollback.
- The root `docker-compose.yml` (Watchtower, autoheal) and the README's QNAP
  section describe self-hosting, not the hosted instance.
- Admins named in `VIRGIL_ADMIN_EMAILS` edit server settings (mail, alert
  webhook, off-site copies, AI key, Plausible, operator, server API key scopes)
  in Settings > Server; the environment is the fallback. A config claim must say
  which source applied.
- API credentials: per-account tokens from Settings > Security, or the server
  `VIRGIL_API_KEY`, which fails closed without `VIRGIL_API_USER_EMAIL`
  (`app/routers/api.py`). `VIRGIL_REQUIRE_TOTP` forces an authenticator.
- Feature flags are per account in Settings and are not exposed over MCP.
- MCP has two entry points: the hosted `/mcp` (`app/mcp_endpoint.py`) and the
  stdio wrapper `mcp_server/virgil_mcp.py` with its own lock. Both call the
  REST API with the caller's credential.

## Production

- Every production run (`scripts/deploy.py`, Ansible, SSH to the host, a
  Settings > Server change) needs the owner's explicit approval of that run.
  Without it, production stays out of your claims.
- Record what was proved, and what was not, in [VERIFICATION.md](VERIFICATION.md)
  or on the owning issue, using
  [docs/post-deploy-evidence.md](docs/post-deploy-evidence.md). A claim made
  only in chat is not a record.

## Security

- Never commit secrets, `.env` files, host metadata or real data. Host
  addresses and keys stay in the password manager and the ignored files that
  `scripts/materialize_ansible_host.py` writes.
- Never quote personal journal, health, finance or school content into issues,
  PRs, commits, fixtures or screenshots. Demo data is synthetic
  (`scripts/seed_demo.py`).
- Report security problems through [SECURITY.md](SECURITY.md).
