# Whole-instance recovery

A complete recovery needs the central registry, every user database it references,
and the original Fernet key. Without the key, stored integration credentials and
encrypted MFA secrets cannot be decrypted. Preserve deployment configuration and
external credentials separately in the operator's secret store.

The operator CLI creates a directory with a versioned manifest and SHA-256
checksums. SQLite copies use the backup API and pass integrity and foreign-key
checks. The registry determines the user database set. Missing databases fail the
operation. Creation and restore publish a complete directory only after validation;
restore refuses an existing destination. Bundles contain sensitive data and the
decryption key, with directory mode 0700 and key/database mode 0600. Checksums
detect corruption, not malicious replacement of the manifest.

## Create a bundle

Stop every application process and automatic updater first. Separate SQLite
snapshots cannot guarantee consistency across databases while account creation,
reset, deletion, or jobs are writing. `--stopped` is an explicit operator assertion;
the CLI cannot verify remote writers have stopped.

For Compose, using the image that matches the installed schema:

```bash
docker compose stop watchtower virgil
docker compose run --rm --no-deps virgil /app/.venv/bin/python scripts/recovery.py \
  create --central /data/virgil-central.db \
  --destination /data/recovery/bundle-2026-10-11 --stopped
docker compose start virgil watchtower
```

Use a new destination name each time. `VIRGIL_ENCRYPTION_KEY` supplies the key if
configured; otherwise the CLI reads `virgil.key` next to the central database.
An explicit `--key-file` overrides both. The CLI never generates a replacement key.
With a local checkout, the equivalent command is `uv run --locked python
scripts/recovery.py create ...`.

## Copy off the application machine

Configure a separately administered destination. For example, copy a completed
bundle over SSH with `rsync -a --chmod=D700,F600` to a restricted backup directory,
then run `scripts/recovery.py verify /path/to/copied-bundle` on the receiving host.
Use encrypted destination storage because the bundle contains the decryption key.
Set retention independently of the application's rotating daily snapshots. Keep
at least one verified recovery point until a newer off-machine copy is verified.

No remote destination or credentials are provisioned by the application. Recording
a successful local round trip does not establish an off-machine backup.

## Verify and restore

```bash
uv run --locked python scripts/recovery.py verify /backup/bundle
uv run --locked python scripts/recovery.py restore /backup/bundle \
  --destination /recovery/new-data
```

Do this with the original application's writers stopped, or on a separate test
host. Point the matching image at the new data directory and preserve the original
directory. The restored central file is named `virgil-central.db`. Ensure the
container UID can read and write its mounted directory. If deployment configuration
sets `VIRGIL_ENCRYPTION_KEY`, it must match the restored bundle's key; otherwise
remove that override so the restored `virgil.key` is used.

Verify `/healthz`, sign in, inspect representative records from each account, and
confirm stored credentials decrypt. Start a drill with external network access
blocked so restored jobs cannot issue provider calls or repeat external effects.
Review queued work and provider subscription state before reconnecting a restored
production instance. Never run two live writers against the same data directory.

## Restore drill record

Record the bundle timestamp, source application version/image digest, verification
result, restored user count, login/MFA result, sample record checks, elapsed restore
time, and operator. Keep real account information outside this public repository.

Automated tests cover a synthetic bundle, decrypted secret recovery, checksum
rejection, missing user files, and refusal to overwrite an existing destination.
The container smoke check also creates and restores a bundle, then signs in to
the restored account using the same image with external networking disabled.
A production drill on a separate host remains an operator acceptance step.

## Daily and pre-migration snapshots

Daily snapshots are under `VIRGIL_BACKUP_DIR`: `backups/` next to `data/` with
the compose file, `data/backups/` without it. Central snapshots are validated before
being considered fresh. Interrupted writes never publish a final filename.
Pre-migration snapshots are under `pre-migration/` inside that directory and retain the
schema version in `*-pre-migration-vNNN.db`. They are not complete bundles and do
not include the encryption key. Restore a consistent matching set when reversing
a schema deployment; an older image alone cannot undo a migration.
