# Verification

What has actually been run against Virgil, newest first: the command, the
revision, the outcome and what it does not prove. Add an entry after a
production release, a host change, a restore drill or any check whose result
someone will rely on later, and link to where the full record lives (an issue
comment written with [the post-deploy template](docs/post-deploy-evidence.md),
a CI run, a PR). Keep host addresses, tailnet names, account details and
personal data out: this file is public.

Coverage, a test count or green CI alone is not an entry
([AGENTS.md](AGENTS.md#this-is-not-evidence)). Restore drills also go to
[the recovery guide](docs/recovery.md#restore-drill-record) and load checks to
[docs/load-check.md](docs/load-check.md).

## Release of ef95495 (#181, 2026-10-28)

- **Revision:** [`ef95495`](https://github.com/krzysztofbury/virgil/commit/ef954959cc7ddc409c83f47036e64cdca454b00e),
  the merge of [#181](https://github.com/krzysztofbury/virgil/pull/181)
  (Plausible snippet accepted in Settings > Server, env AI key dropped from
  `infra/compose/virgil.env.tpl`). Main
  [CI run](https://github.com/krzysztofbury/virgil/actions/runs/40726524260)
  succeeded.
- **Command:** `scripts/deploy.py release` with that SHA, run by the owner.
- **Outcome:** the owner's
  [phase 6 comment on #174](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6475068818)
  lists #181 among the releases since the move.
- **Not proved by this record:** no command output was captured, so neither
  the script's own public check nor `/healthz` after the release is on record.
  Nothing records that `deploy.py env` re-rendered `/etc/virgil/virgil.env`
  from the new template, or that a pasted Plausible snippet was accepted on
  production.

## Release of 7114520 with central migration 010 (#180, 2026-10-28)

- **Revision:** [`7114520`](https://github.com/krzysztofbury/virgil/commit/7114520fcc658509081d48200a7405f2f93caa32),
  the merge of [#180](https://github.com/krzysztofbury/virgil/pull/180): server
  settings in the app (#179), legal pages and signup consent, and central
  migration 010 (`server_settings`, `server_llm_providers`,
  `users.terms_accepted_at`). Main
  [CI run](https://github.com/krzysztofbury/virgil/actions/runs/40718682769)
  succeeded.
- **Before the merge:** migration 010 ran as a dry run on a copy of the
  production registry (`integrity_check` ok, accounts unchanged); the PR
  reports 1625 passed for the full suite without naming the command; Settings >
  Server and Users, phone width, the
  signup consent in both languages and the privacy page were checked in a
  browser. All from the [PR description](https://github.com/krzysztofbury/virgil/pull/180).
- **Command:** `scripts/deploy.py release` with that SHA, run by the owner.
- **Outcome:** listed among the releases since the move in the
  [phase 6 comment on #174](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6475068818).
- **Not proved by this record:** that migration 010 applied on the live
  registry (only the dry run on a copy is recorded), the startup migration
  line, `/healthz` after the release, the script's public check, or any
  Settings > Server change on production.

## Cutover follow-up: Oura and MCP clients (2026-10-28)

- **Revision:** not recorded; the
  [comment](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6475068818)
  was posted after both releases above.
- **Outcome:** `VIRGIL_BASE_URL` is `https://virgil.pl`. After the owner changed
  the Oura redirect URI, scheduled and manual Oura syncs succeeded. The stdio
  wrapper and the hosted `/mcp` endpoint (`initialize`, `tools/list`) answered
  without Cloudflare Access credentials, and the owner's chat bot client works.
- **Not proved:** an MCP tool call on a changed path (only `initialize` and
  `tools/list`), the Oura webhook delivery path.

## Restore drill from the first off-site copy (#174, 2026-10-28)

- **Revision:** the instance at `042d64e`, right after the data move.
- **Command:** the first off-site run uploaded 4 files to R2; the drill
  downloaded that copy, then ran `unseal`, `verify` and `restore`
  ([docs/recovery.md](docs/recovery.md#restore-from-an-off-site-copy)).
- **Outcome:** all three passed, the restored encryption key matches the old
  instance's key, and both restored databases pass `integrity_check`
  ([comment](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6470995605)).
- **Not proved:** that the restored directory starts and serves pages, password
  and MFA sign-in, and the elapsed time. The
  [2026-10-21 drill](docs/recovery.md#2026-10-21-off-site-copy-restored-on-a-development-machine-47)
  covered startup and page rendering for an earlier bundle. This drill is not
  in the recovery guide's drill record, which needs those fields.

## Data move to the new host (#174, 2026-10-28)

- **Revision:** [`042d64e`](https://github.com/krzysztofbury/virgil/commit/042d64e1b31a528b7628a226f56e96baa437f6d6)
  on the new host.
- **Command:** the old container was stopped and its data directory copied with
  a SHA-256 manifest, checked on the new host; databases were checked with
  `integrity_check` before the move; the encryption key file moved with them.
- **Outcome:** every file matched the manifest. The new instance started with
  every user database migrated and none failed. `virgil.pl` is routed through
  the new tunnel and serves `042d64e`; HTTP redirects to HTTPS, TLS 1.3 is the
  minimum and the app sends HSTS. The public check in `deploy.py` gained a
  User-Agent, because Cloudflare answered 403 to the default one
  ([comment](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6470995605)).
- **Not proved:** `integrity_check` on the copies after the move (the matching
  checksums stand in for it), or sign-in by each account.

## First release on the new host (#174, 2026-10-28)

- **Revision:** `042d64e`, main
  [CI run](https://github.com/krzysztofbury/virgil/actions/runs/40544757110)
  succeeded; the instance was empty and had no public route yet.
- **Command:** `scripts/deploy.py env`, then
  `scripts/deploy.py release --no-public-check`.
- **Outcome:** both env files written (mode 0600, root). The release by digest
  took about 2 minutes; Compose reported the container healthy, central
  migrations 001 to 009 applied and `/healthz` returned 200. Virgil used 280 MiB
  of its 640 MiB limit, cloudflared 15 of 96, the host 449 of 1024 MiB; disk
  25% used. cloudflared registered four tunnel connections,
  `virgil-heal.timer` is active and nothing listens outside loopback
  ([comment](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6470180163)).
- **Not proved:** the public route and the script's public check (skipped by
  the flag), behaviour with real data, off-site copies (off until the data
  move).

## Host baseline and tailnet cutover (#174, 2026-10-28)

- **Revision:** `infra/ansible/` and `infra/tailscale/policy.hujson` on the
  [#180](https://github.com/krzysztofbury/virgil/pull/180) branch, before its
  merge.
- **Command:** the provider's break-glass runner was proven first; then
  `site.yml`, a second `site.yml --check` run, and `ssh-tailnet.yml` with
  `ssh_tailnet_stage=join` and then `private`
  ([infra/ansible/README.md](infra/ansible/README.md)).
- **Outcome:** the check-mode rerun changes nothing but the APT index refresh;
  password and keyboard-interactive logins are refused. The host joined the
  tailnet with a tagged one-time key that left no copy on the host or the
  workstation. `sshd` listens on loopback only, UFW is active with no inbound
  rule, and the provider's public SSH port times out from outside. The first
  apply found two baseline fixes: the provider image's default `ufw allow 22`
  rule, and `/run/sshd` missing during validation after a package upgrade
  restarted `ssh.service`
  ([comment](https://github.com/krzysztofbury/virgil/issues/174#issuecomment-6466317465)).
- **Not proved:** the public IPv6 path from outside; the operator's network has
  no IPv6 route. With no listener beyond loopback and no allow rule it is
  closed on the host side only.
