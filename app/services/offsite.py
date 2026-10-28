"""Encrypted off-machine copies in S3-compatible storage, such as Cloudflare R2.

Once a day the scheduler builds a recovery bundle (app/recovery.py) while the
application runs, each database consistent on its own like the daily snapshots,
and uploads it under daily/<run>/: every file separately, compressed, then
encrypted to an age public key. Only that public key is on this machine, so what
the bucket holds is unreadable without the secret key kept elsewhere. The first
complete run of each month is also copied to monthly/<YYYY-MM>/. How long either
is kept is the bucket's lifecycle rules (docs/recovery.md), not this code.

One object per file so an account's copies can go with it: deleting or resetting
an account queues the removal of its file from every run, as it deletes the local
backups. manifest.json goes last, so a run without one is incomplete.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import config
from app.formatting import time_ago
from app.recovery import SEALED_SUFFIX, USER_FILE, create, parse_recipient, seal
from app.server_settings import current
from app.services import backup

logger = logging.getLogger(__name__)

DAILY_PREFIX = "daily/"
MONTHLY_PREFIX = "monthly/"
UPLOAD_INTERVAL = timedelta(hours=24)
# After a failure: soon enough to recover from a blip the same day, rarely
# enough not to hammer a provider that is down.
RETRY_AFTER = timedelta(hours=1)
ALERT_EVERY = timedelta(hours=24)
# One missed day is a retry; two mean nothing recent is off the machine.
OVERDUE_AFTER = timedelta(hours=48)

_SETTINGS = {
    "VIRGIL_OFFSITE_ENDPOINT": "OFFSITE_ENDPOINT",
    "VIRGIL_OFFSITE_BUCKET": "OFFSITE_BUCKET",
    "VIRGIL_OFFSITE_ACCESS_KEY_ID": "OFFSITE_ACCESS_KEY_ID",
    "VIRGIL_OFFSITE_SECRET_ACCESS_KEY": "OFFSITE_SECRET_ACCESS_KEY",  # pragma: allowlist secret
    "VIRGIL_OFFSITE_RECIPIENT": "OFFSITE_RECIPIENT",
}

_lock = asyncio.Lock()


class OffsiteConfigError(ValueError):
    pass


@dataclass(frozen=True)
class OffsiteSettings:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    recipient: object = field(repr=False)


def offsite_settings() -> OffsiteSettings | None:
    """The configured destination, None when off, OffsiteConfigError when half set."""
    values = {name: getattr(current, attribute) for name, attribute in _SETTINGS.items()}
    if not any(values.values()):
        return None
    missing = [name for name, value in values.items() if not value]
    if missing:
        from app import server_settings

        # Name where the gap is: an empty value saved in the app hides the
        # environment's, so pointing at the variable alone would mislead.
        where = [
            f"{name} (empty in Settings > Server)" if server_settings.source(_SETTINGS[name]) == "app" else name
            for name in missing
        ]
        raise OffsiteConfigError(f"Off-site copies are off: {', '.join(where)} not set")
    if not values["VIRGIL_OFFSITE_ENDPOINT"].startswith("https://"):
        raise OffsiteConfigError("Off-site copies are off: VIRGIL_OFFSITE_ENDPOINT must be an https:// URL")
    try:
        recipient = parse_recipient(values["VIRGIL_OFFSITE_RECIPIENT"])
    except ValueError:
        raise OffsiteConfigError("Off-site copies are off: VIRGIL_OFFSITE_RECIPIENT is not an age public key") from None
    return OffsiteSettings(
        endpoint=values["VIRGIL_OFFSITE_ENDPOINT"],
        bucket=values["VIRGIL_OFFSITE_BUCKET"],
        access_key_id=values["VIRGIL_OFFSITE_ACCESS_KEY_ID"],
        secret_access_key=values["VIRGIL_OFFSITE_SECRET_ACCESS_KEY"],
        recipient=recipient,
    )


def _client(settings: OffsiteSettings):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
        config=Config(
            # Only the checksums S3 itself requires: R2 does not take every
            # default newer boto3 versions add.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"mode": "standard", "max_attempts": 5},
            connect_timeout=10,
            read_timeout=120,
        ),
    )


def _state_dir() -> Path:
    # Resolved at call time, like every backup path, so tests can move it.
    return backup.BACKUP_DIR / "offsite"


def _pending_dir() -> Path:
    return _state_dir() / "pending-deletions"


def read_state() -> dict:
    try:
        state = json.loads((_state_dir() / "state.json").read_text())
    except OSError, ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(state: dict) -> None:
    directory = _state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".state.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(temporary, directory / "state.json")


def queue_deletion(db_filename: str) -> None:
    """Remember to delete one account's file from every off-site run.

    A marker file, not a call to the bucket: an account deletion must not wait
    on, or fail with, the storage provider. The scheduler retries until done.
    """
    try:
        configured = offsite_settings() is not None
    except OffsiteConfigError:
        configured = True
    # Turned off or half set now does not mean no copies were made before: the
    # marker waits until copies are configured again.
    if not configured and not _state_dir().exists():
        return
    if not USER_FILE.fullmatch(db_filename):
        raise ValueError(f"Invalid db_filename: {db_filename}")
    directory = _pending_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / db_filename).touch()


def _pending_deletions() -> list[str]:
    directory = _pending_dir()
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir() if USER_FILE.fullmatch(path.name))


def _list_keys(client, bucket: str, prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        page = client.list_objects_v2(Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
        keys.extend(item["Key"] for item in page.get("Contents", []))
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


def _delete_account_copies(settings: OffsiteSettings, names: list[str]) -> int:
    client = _client(settings)
    endings = tuple(f"/users/{name}{SEALED_SUFFIX}" for name in names)
    doomed = [
        key
        for prefix in (DAILY_PREFIX, MONTHLY_PREFIX)
        for key in _list_keys(client, settings.bucket, prefix)
        if key.endswith(endings)
    ]
    for start in range(0, len(doomed), 1000):
        batch = doomed[start : start + 1000]
        result = client.delete_objects(
            Bucket=settings.bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True}
        )
        if result.get("Errors"):
            raise RuntimeError(f"{len(result['Errors'])} off-site object(s) were not deleted")
    return len(doomed)


def _run_name(now: datetime) -> str:
    # UTC, sortable: the newest run is the last one listed.
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _upload_run(settings: OffsiteSettings, now: datetime, monthly: str | None) -> dict:
    """Build, encrypt and upload one run; copy it to monthly/ when asked. Blocking."""
    client = _client(settings)
    run = _run_name(now)
    workspace = backup.BACKUP_DIR
    workspace.mkdir(parents=True, exist_ok=True)
    # Left by a run the container was killed in: plain database copies. Only
    # this job writes these, and it holds the lock.
    for stale in workspace.glob(".offsite-*"):
        shutil.rmtree(stale, ignore_errors=True)
    keys, total = [], 0
    with tempfile.TemporaryDirectory(prefix=".offsite-", dir=workspace) as temporary:
        bundle = Path(temporary) / "bundle"
        create(Path(config.CENTRAL_DB_PATH), bundle)
        names = sorted(str(path.relative_to(bundle)) for path in bundle.rglob("*") if path.is_file())
        names.remove("manifest.json")
        names.append("manifest.json")
        for name in names:
            sealed = Path(temporary) / "sealed"
            seal(bundle / name, sealed, settings.recipient)
            size = sealed.stat().st_size
            key = f"{DAILY_PREFIX}{run}/{name}{SEALED_SUFFIX}"
            client.upload_file(str(sealed), settings.bucket, key)
            if client.head_object(Bucket=settings.bucket, Key=key)["ContentLength"] != size:
                raise RuntimeError(f"Off-site object {key} does not match the uploaded size")
            sealed.unlink()
            keys.append(key)
            total += size
    if monthly:
        # Server-side copies, manifest last again.
        for key in keys:
            client.copy_object(
                Bucket=settings.bucket,
                Key=f"{MONTHLY_PREFIX}{monthly}/" + key.removeprefix(f"{DAILY_PREFIX}{run}/"),
                CopySource={"Bucket": settings.bucket, "Key": key},
            )
    return {"run": run, "files": len(keys), "bytes": total}


def _describe(exc: Exception) -> str:
    """What went wrong, short and with nothing secret in it."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        return f"{type(exc).__name__} {response['Error'].get('Code', '')}".strip()
    if isinstance(exc, ValueError | OSError | RuntimeError):
        return f"{type(exc).__name__}: {exc}"[:200]
    return type(exc).__name__


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _upload_due(state: dict, now: datetime) -> bool:
    if state.get("upload_now"):
        return True
    last_success = _parse(state.get("last_success_at"))
    if last_success is not None and now - last_success < UPLOAD_INTERVAL:
        return False
    last_failure = _parse(state.get("last_failure_at"))
    return last_failure is None or now - last_failure >= RETRY_AFTER


async def _fail(state: dict, now: datetime, problem: str) -> None:
    state["last_failure_at"] = now.isoformat()
    state["last_error"] = problem
    last_alert = _parse(state.get("last_alert_at"))
    if current.ALERT_WEBHOOK_URL and (last_alert is None or now - last_alert >= ALERT_EVERY):
        from app.alerts import send_alert

        last_success = _parse(state.get("last_success_at"))
        newest = f"is {time_ago(last_success, now)}" if last_success else "does not exist yet"
        if await send_alert(
            current.ALERT_WEBHOOK_URL,
            f"Virgil could not make its off-site copy ({problem}). The newest one {newest}. "
            "It retries every hour; Settings > Data shows the state.",
        ):
            state["last_alert_at"] = now.isoformat()
    _write_state(state)


async def offsite_tick(now: datetime | None = None) -> None:
    """One scheduler pass: pending account deletions, then the daily run when due."""
    try:
        settings = offsite_settings()
    except OffsiteConfigError:
        # Reported once at startup and in Settings, not every minute.
        return
    if settings is None:
        return
    now = now or datetime.now(UTC)
    async with _lock:
        state = read_state()
        last_failure = _parse(state.get("last_failure_at"))
        if last_failure is not None and now - last_failure < RETRY_AFTER:
            return

        pending = await asyncio.to_thread(_pending_deletions)
        if pending:
            try:
                deleted = await asyncio.to_thread(_delete_account_copies, settings, pending)
            except Exception as exc:
                logger.exception("Deleting off-site copies of %d account(s) failed", len(pending))
                await _fail(state, now, _describe(exc))
                return
            for name in pending:
                (_pending_dir() / name).unlink(missing_ok=True)
            logger.info("Deleted %d off-site object(s) of %d account(s)", deleted, len(pending))
            # The runs that held those files no longer restore as a whole: make
            # a complete one now rather than tomorrow.
            state["upload_now"] = True
            _write_state(state)

        if not _upload_due(state, now):
            return
        month = now.astimezone(UTC).strftime("%Y-%m")
        try:
            result = await asyncio.to_thread(
                _upload_run, settings, now, month if state.get("monthly") != month else None
            )
        except Exception as exc:
            logger.exception("Off-site copy failed")
            await _fail(state, now, _describe(exc))
            return
        state.update(
            last_success_at=now.isoformat(),
            last_run=result["run"],
            last_files=result["files"],
            last_bytes=result["bytes"],
            monthly=month,
            upload_now=False,
            last_failure_at=None,
            last_error=None,
        )
        _write_state(state)
        logger.info("Off-site copy %s uploaded: %d file(s), %d bytes", result["run"], result["files"], result["bytes"])


def offsite_status(now: datetime | None = None) -> dict:
    """What Settings tells an administrator about off-site copies."""
    now = now or datetime.now(UTC)
    try:
        settings = offsite_settings()
    except OffsiteConfigError as exc:
        return {"state": "misconfigured", "problem": str(exc)}
    if settings is None:
        return {"state": "off"}
    state = read_state()
    last_success = _parse(state.get("last_success_at"))
    status = {
        "bucket": settings.bucket,
        "latest_age": time_ago(last_success, now) if last_success else None,
        "latest_files": state.get("last_files"),
        "latest_megabytes": round((state.get("last_bytes") or 0) / 1_000_000, 1),
        "problem": state.get("last_error"),
        "pending_deletions": len(_pending_deletions()),
    }
    if state.get("last_error"):
        status["state"] = "failed"
    elif last_success is None:
        status["state"] = "none"
    elif now - last_success > OVERDUE_AFTER:
        status["state"] = "overdue"
    else:
        status["state"] = "ok"
    return status
