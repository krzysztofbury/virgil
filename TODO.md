# Virgil roadmap

Current release: 0.7.0. Shipped work belongs in [CHANGELOG.md](CHANGELOG.md).
Release procedure: [RELEASE.md](RELEASE.md).
Recovery procedure: [docs/recovery.md](docs/recovery.md).

## Reliability review completed in 0.7.0

- [x] Prevent version-tag builds from changing the production `latest` pointer.
- [x] Consolidate CI, eliminate duplicate branch/PR checks, and use locked tools.
- [x] Upgrade vulnerable dependencies and audit both application and MCP locks.
- [x] Qualify the actual container and independently locked MCP entry point.
- [x] Remove date-dependent fixtures and mutable LiteLLM metadata from tests.
- [x] Validate and atomically publish central backups; recover from corrupt copies.
- [x] Add complete operator recovery bundles and restore into a new directory.
- [x] Require an explicit registered account for markdown imports.
- [x] Remove the legacy global database connection and correct Settings' path.
- [x] Share library persistence and transactions across form and REST writes.

## Next: recovery and data ownership

Central migrations, mutation feedback, durable jobs, and provider subscription
reconciliation are complete. Recovery is no longer blocked by those phases.

- [ ] Configure an off-machine recovery destination and retention policy.
- [ ] Perform and record an operator restore drill on a separate host using a
      production backup. Local synthetic round trips do not satisfy this item.
- [ ] Add self-service, validated per-user restore and a versioned JSON transfer
      format, with a dry-run report and an export/import round-trip test.
- [ ] Show backup age and status in Settings, including a backup prompt before reset.

## Offline behavior and accessibility

- [ ] Add a persistent read-only offline banner and disable mutation controls.
      Authenticated HTML stays network-only; no offline write queue is planned.
- [ ] Complete keyboard navigation, reduced motion, skip-to-content, and chart
      text/table equivalents. Verify in a real desktop and mobile browser.

## Correctable history

- [ ] Allow blood results, PMO events, workout entries, and experiment-day details
      to be corrected without SQL. Include validation and deliberate deletion.
- [ ] Add goal deletion undo and bounded metadata-only change history.

## Longitudinal insights

- [ ] Implement the actual 12-week Daily heatmap.
- [ ] Add consistent 4/12/26-week ranges, Life Score history, and Oura freshness.

## Conditional product work

- [ ] Accounts outside a trusted household: decide the support scope before
      implementing invites, reset codes, scoped per-client tokens, and access audit.
- [ ] Add EN/PL translations and locale-aware seeds after choosing one i18n approach.
- [ ] Add another wearable only after confirming the operational Oura baseline.
- [ ] Revisit billing and cloud-managed backups only with an explicit SaaS decision.

## Remaining deployment work

- [ ] Record representative upload and background-job load verification on the
      deployment hardware after the web-stack upgrade.
- [ ] Self-host or integrity-pin CDN assets and reduce CSP inline/eval allowances.
- [ ] Optionally add deployment notifications.

Watchtower remains best-effort auto-update with manual rollback. CI image smoke
checks do not turn it into a health-gated deployment system. The maintained
recovery procedure explains the key, registry, user-data, and migration boundaries.
