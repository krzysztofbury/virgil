# Whole-instance recovery

A complete recovery needs the central registry, every user database it references,
and the original Fernet key. Without the key, stored integration credentials and
encrypted MFA secrets cannot be decrypted. Preserve deployment configuration and
external credentials separately in the operator's secret store.

The operator CLI (`scripts/recovery.py`) creates a directory with a versioned manifest and SHA-256
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
  --destination /backups/recovery/bundle-2026-10-11 --stopped
docker compose start virgil watchtower
```

Use a new destination name each time. `VIRGIL_ENCRYPTION_KEY` supplies the key if
configured; otherwise the CLI reads `virgil.key` next to the central database.
An explicit `--key-file` overrides both. The CLI never generates a replacement key.
With a local checkout, the equivalent command is `uv run --locked python
scripts/recovery.py create ...`.

## Off-site copies

With the five `VIRGIL_OFFSITE_*` settings (`.env.example`), the application uploads
an encrypted copy of the whole instance once a day to an S3-compatible bucket such
as Cloudflare R2. It needs no stop: each database is copied with the SQLite backup
API, consistent on its own like the daily snapshots, and the copy passes the same
checks as a bundle made with `create` before anything is uploaded. A failed run is
retried every hour, and `ALERT_WEBHOOK_URL` hears about it at most once a day.
Settings > Automation shows administrators the newest copy and the last problem.

Each run is a bundle stored one file per object, compressed and then encrypted to
an [age](https://age-encryption.org) public key:

```text
daily/<run>/manifest.json.gz.age
daily/<run>/virgil-central.db.gz.age
daily/<run>/virgil.key.gz.age
daily/<run>/users/<account>.db.gz.age
monthly/<YYYY-MM>/...        the month's first complete run, copied in the bucket
```

`<run>` is the UTC time it started, so the newest sorts last. The manifest is
uploaded last: a run without one is incomplete and is not a recovery point.
Only the public key is on the application machine. The secret key opens every
copy, so keep it in a password manager, not next to the data; losing it loses
every off-site copy. `scripts/recovery.py keygen --identity-file <new file>`
(or `age-keygen`) makes a pair.

Deleting or resetting an account queues the removal of its file from every run,
daily and monthly. The scheduler sends it within a minute, retrying while the
bucket is unreachable, then uploads a complete run straight away, because the
older runs no longer match their registry. Those older runs still hold the
deleted account's registry row until they expire.

### Credentials and retention

Use credentials that reach only this bucket: for R2, an API token with Object
Read & Write on it. Removing an account's copies needs delete, so a bucket lock
(retention lock) would block it; do not add one.

Retention is the bucket's lifecycle rules, not the application. For R2, under the
bucket's Settings > Object lifecycle rules:

| Rule | Prefix | Action |
| --- | --- | --- |
| Daily copies | `daily/` | Delete objects 30 days after upload |
| Monthly copies | `monthly/` | Delete objects 365 days after upload |
| Unfinished uploads | (whole bucket) | Abort incomplete multipart uploads after 1 day |

That keeps the last 30 daily runs and 12 monthly ones. The rules delete by age
only: if uploads stop, the daily copies run out after 30 days, which is why a
failure alerts and Settings marks a copy older than two days as overdue.

### Restore from an off-site copy

Download one run's directory, for example with the AWS CLI and the same
credentials, then decrypt it into a bundle and restore that as below:

```bash
aws s3 cp --recursive s3://<bucket>/daily/<run>/ ./copy \
  --endpoint-url https://<account id>.r2.cloudflarestorage.com
uv run --locked python scripts/recovery.py unseal ./copy \
  --identity identity.txt --destination ./bundle
uv run --locked python scripts/recovery.py restore ./bundle --destination /recovery/new-data
```

`unseal` refuses a file it does not expect, one that does not decrypt, and a
result that fails `verify`. Encryption keeps the copies private but does not
prove who wrote them: anyone holding the bucket's write credentials and the
public key could upload a run. Restore a run whose time you expect, and replace
the bucket credentials first if they may have leaked. One account's database alone is
`age -d -i identity.txt users/<account>.db.gz.age | gunzip > <account>.db`.

## Copy a bundle off the machine by hand

A bundle made with `create` can also be copied over SSH with
`rsync -a --chmod=D700,F600` to a restricted backup directory, then checked with
`scripts/recovery.py verify /path/to/copied-bundle` on the receiving host. Use
encrypted destination storage, because the bundle holds the decryption key in
the clear. Keep at least one verified recovery point until a newer off-machine
copy is verified. Recording a successful local round trip does not establish an
off-machine backup.

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
This matters at once: the scheduler queues an Oura sync within the first minute,
and with network access it would refresh the Oura token. Oura's refresh tokens
are single-use, so the production instance could lose its connection.
Review queued work and provider subscription state before reconnecting a restored
production instance. Never run two live writers against the same data directory.

### A drill on another machine

With the bundle restored to `./data`, run the production image on it with no
network, then check it from inside the container:

```bash
mkdir -m 700 backups
docker run -d --name virgil-drill --network none -u 1000:1000 \
  -v "$PWD/data:/data" -v "$PWD/backups:/backups" \
  -e VIRGIL_ENV=prod -e VIRGIL_CENTRAL_DB_PATH=/data/virgil-central.db \
  -e VIRGIL_BACKUP_DIR=/backups -e VIRGIL_BASE_URL=http://localhost:8123 \
  ghcr.io/krzysztofbury/virgil:latest
docker exec virgil-drill python -c "import urllib.request as u; print(u.urlopen('http://localhost:8123/healthz').read())"
docker logs virgil-drill | grep "user migrations"
```

With no network there is also no published port, so pages are checked from
inside too. A session for a restored account can be signed with the restored
key (`app.auth.create_session(user_id, session_epoch)`, sent as the
`virgil_session` cookie): it shows the pages render from the restored data
without anyone's password. Signing in with the password and MFA needs a
reachable instance, which means network access, so do it only once queued jobs
and provider connections are reviewed. Remove the container and the data
afterwards: `docker rm -f virgil-drill`, then delete the directories.

## Restore drill record

Record the bundle timestamp, source application version/image digest, verification
result, restored user count, login/MFA result, sample record checks, elapsed restore
time, and operator. Keep real account information outside this public repository.

### 2026-10-21: off-site copy restored on a development machine (#47)

- **Source:** the newest daily run in R2 (`2026-10-21T102141Z`), made by the
  production instance at `e651759`. It was downloaded with the S3 API, and
  `unseal`, `verify` and `restore` all passed.
- **Image:** production `47fc1de`, run with `--network none` on the restored
  directory.
- **Result:** `/healthz` 200 and all migrations ok. Every account in the registry
  was restored, and its record counts were plausible for its history. Every
  stored credential decrypted with the restored key. Dashboard, Daily,
  Training, Oura, Blood, Goals, Experiments and Settings rendered for the
  restored account through a session signed with the restored key.
- **Time:** about 10 s from the download to a healthy server: the download
  0.4 s, unseal, verify and restore about 1 s, and startup 6.4 s.
- **Not exercised:** password and MFA sign-in, which needs a reachable instance
  (see above).
- **Gaps found:**
  - The guide had no drill commands. They are added above.
  - The urgency of the network block (the Oura sync within a minute) was not
    stated. It is stated above now.

Automated tests cover a synthetic bundle, decrypted secret recovery, checksum
rejection, missing user files, and refusal to overwrite an existing destination.
The container smoke check also creates and restores a bundle, then signs in to
the restored account using the same image with external networking disabled.
Repeat a drill on another machine after any change to the bundle format or the
restore path, and record it here.

## Daily and pre-migration snapshots

Daily snapshots are under `VIRGIL_BACKUP_DIR`: `backups/` next to `data/` with
the compose file, `data/backups/` without it. Central snapshots are validated before
being considered fresh. Interrupted writes never publish a final filename.
Pre-migration snapshots are under `pre-migration/` inside that directory and retain the
schema version in `*-pre-migration-vNNN.db`. They are not complete bundles and do
not include the encryption key. Restore a consistent matching set when reversing
a schema deployment; an older image alone cannot undo a migration.
