# Post-deploy evidence

Copy this into a comment on the owning issue after a production release or a
production configuration change, then add a short entry to
[VERIFICATION.md](../VERIFICATION.md) that links to it. Leave a line out only
when it does not apply, and say why. Every production run needs the owner's
explicit approval first ([AGENTS.md](../AGENTS.md#production)).

Keep host addresses, tailnet names, account emails, tokens and personal data
out of the comment: the repository and its issues are public.

```markdown
**Release:** <full SHA> (#PR), CI run <link>
**Command:** `uv run python scripts/deploy.py release <full SHA>`, approved by the owner
**Image:** digest from the command output or `VIRGIL_IMAGE` in `/opt/virgil/release.env`
**Previous pin:** from `/opt/virgil/release.env.previous`

- [ ] `deploy.py` ended without error, including its public check (`/login`
      serves `/static/css/app.css?v=<sha>`), or `--no-public-check` and why.
- [ ] `/livez`: <status>. Liveness only.
- [ ] `/healthz`: <status>. Readiness; 503 means a database failed its migrations.
- [ ] Startup log line `Virgil version=<sha>`: the central migration number
      and `user migrations OK for N DB(s), 0 failed`.
- [ ] One authenticated web path for the changed feature: <page, what was seen>.
- [ ] One API or MCP call on the changed path, when relevant: <tool or
      endpoint, hosted `/mcp` or stdio, result>.
- [ ] Configuration the change depends on: <setting>, and whether Settings >
      Server or the environment supplied it (`VIRGIL_API_USER_EMAIL`, feature
      flags, mail, off-site copies, ...).
- [ ] Memory against the container `mem_limit` and host disk, for a change that
      could move them.
- [ ] Alerts: <webhook message received, or none expected>.

**Not exercised:** <what this does not prove, for example password and MFA
sign-in, load, another account's data, a path behind a feature flag>.
```

A green `/livez`, a successful image push or green CI on `main` is not evidence
that production serves the change; see [AGENTS.md](../AGENTS.md#this-is-not-evidence).
Load-sensitive changes also need [the load check](load-check.md), and a change
to the bundle format or the restore path needs a new
[restore drill](recovery.md#restore-drill-record).
