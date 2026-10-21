"""Off-site copies: encrypted per-file runs, account deletion reaching the bucket, and retry pacing.

The bucket is a stand-in at the boto3 client boundary; the databases, bundle
checks and age encryption are real, and every restore goes through unseal().
"""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet
from pyrage import x25519

from app import config
from app.recovery import unseal
from app.services import backup, offsite

ONE = "00000000-0000-0000-0000-000000000001.db"
TWO = "00000000-0000-0000-0000-000000000002.db"
DAY = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)


class FakeBucket:
    """The S3 calls the off-site job makes, against a dict. Lists two keys a page."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.failure: str | None = None

    def _check(self, operation):
        if self.failure:
            raise ClientError({"Error": {"Code": self.failure, "Message": "denied"}}, operation)

    def upload_file(self, path, bucket, key):
        self._check("PutObject")
        with open(path, "rb") as handle:
            self.objects[key] = handle.read()

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key])}

    def copy_object(self, Bucket, Key, CopySource):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        self._check("ListObjectsV2")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + 2]
        more = start + 2 < len(keys)
        return {
            "Contents": [{"Key": k} for k in page],
            "IsTruncated": more,
            **({"NextContinuationToken": str(start + 2)} if more else {}),
        }

    def delete_objects(self, Bucket, Delete):
        for item in Delete["Objects"]:
            self.objects.pop(item["Key"], None)
        return {}

    def runs(self, prefix="daily/"):
        return sorted({k.removeprefix(prefix).split("/")[0] for k in self.objects if k.startswith(prefix)})

    def download(self, prefix, directory):
        for key, body in self.objects.items():
            if key.startswith(prefix):
                path = directory / key.removeprefix(prefix)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
        return directory


@pytest.fixture
def instance(tmp_path, monkeypatch):
    """A two-account instance with off-site copies configured to a fake bucket."""
    monkeypatch.delenv("VIRGIL_ENCRYPTION_KEY", raising=False)
    data = tmp_path / "data"
    (data / "users").mkdir(parents=True)
    (data / "virgil.key").write_bytes(Fernet.generate_key())
    with sqlite3.connect(data / "virgil-central.db") as db:
        db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, db_filename TEXT)")
        db.executemany("INSERT INTO users VALUES (?, ?)", [("one", ONE), ("two", TWO)])
    for name, note in ((ONE, "first account"), (TWO, "second account")):
        with sqlite3.connect(data / "users" / name) as db:
            db.execute("CREATE TABLE notes (body TEXT)")
            db.execute("INSERT INTO notes VALUES (?)", (note,))

    identity = x25519.Identity.generate()
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text(f"# test key\n{identity}\n")
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", str(data / "virgil-central.db"))
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "OFFSITE_ENDPOINT", "https://account.r2.example")
    monkeypatch.setattr(config, "OFFSITE_BUCKET", "virgil")
    monkeypatch.setattr(config, "OFFSITE_ACCESS_KEY_ID", "id")
    monkeypatch.setattr(config, "OFFSITE_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(config, "OFFSITE_RECIPIENT", str(identity.to_public()))
    monkeypatch.setattr(config, "ALERT_WEBHOOK_URL", "https://hooks.example/alert")
    alerts: list[str] = []

    async def capture(url, text):
        alerts.append(text)
        return True

    monkeypatch.setattr("app.alerts.send_alert", capture)
    bucket = FakeBucket()
    monkeypatch.setattr(offsite, "_client", lambda settings: bucket)
    return {"data": data, "bucket": bucket, "identity": identity_file, "alerts": alerts, "tmp": tmp_path}


def _notes(path):
    with sqlite3.connect(path) as db:
        return [row[0] for row in db.execute("SELECT body FROM notes")]


def test_a_run_restores_every_account_and_only_with_the_secret_key(instance):
    bucket, tmp = instance["bucket"], instance["tmp"]
    asyncio.run(offsite.offsite_tick(DAY))

    (run,) = bucket.runs()
    keys = sorted(k for k in bucket.objects if k.startswith(f"daily/{run}/"))
    assert [k.removeprefix(f"daily/{run}/") for k in keys] == [
        "manifest.json.gz.age",
        f"users/{ONE}.gz.age",
        f"users/{TWO}.gz.age",
        "virgil-central.db.gz.age",
        "virgil.key.gz.age",
    ]
    # Nothing is stored readable, the encryption key least of all.
    key = (instance["data"] / "virgil.key").read_bytes()
    assert all(key not in body and b"first account" not in body for body in bucket.objects.values())

    restored = tmp / "restored"
    unseal(bucket.download(f"daily/{run}/", tmp / "download"), instance["identity"], restored)
    assert _notes(restored / "users" / ONE) == ["first account"]
    assert (restored / "virgil.key").read_bytes() == key

    stranger = tmp / "stranger.txt"
    stranger.write_text(f"{x25519.Identity.generate()}\n")
    with pytest.raises(ValueError, match="does not decrypt"):
        unseal(tmp / "download", stranger, tmp / "refused")
    assert not (tmp / "refused").exists()

    # The month's first run is also kept as the monthly copy.
    assert bucket.runs("monthly/") == ["2026-10"]
    unseal(bucket.download("monthly/2026-10/", tmp / "monthly"), instance["identity"], tmp / "from-monthly")


def test_runs_once_a_day_and_once_a_month(instance):
    bucket = instance["bucket"]
    asyncio.run(offsite.offsite_tick(DAY))
    asyncio.run(offsite.offsite_tick(DAY + timedelta(hours=23)))
    assert len(bucket.runs()) == 1

    asyncio.run(offsite.offsite_tick(DAY + timedelta(hours=24)))
    assert len(bucket.runs()) == 2
    assert bucket.runs("monthly/") == ["2026-10"]
    assert sum(k.startswith("monthly/") for k in bucket.objects) == 5

    asyncio.run(offsite.offsite_tick(datetime(2026, 11, 1, 3, 0, tzinfo=UTC)))
    assert bucket.runs("monthly/") == ["2026-10", "2026-11"]
    assert offsite.offsite_status(datetime(2026, 11, 1, 4, 0, tzinfo=UTC))["state"] == "ok"


def test_a_failure_alerts_once_and_retries_hourly(instance):
    bucket, alerts = instance["bucket"], instance["alerts"]
    bucket.failure = "AccessDenied"
    asyncio.run(offsite.offsite_tick(DAY))
    assert bucket.runs() == []
    assert len(alerts) == 1
    assert "AccessDenied" in alerts[0] and "does not exist yet" in alerts[0]
    assert "secret" not in alerts[0]
    status = offsite.offsite_status(DAY)
    assert status["state"] == "failed" and "AccessDenied" in status["problem"]

    asyncio.run(offsite.offsite_tick(DAY + timedelta(minutes=59)))
    asyncio.run(offsite.offsite_tick(DAY + timedelta(minutes=61)))
    assert len(alerts) == 1  # still failing, but already told within the day

    bucket.failure = None
    asyncio.run(offsite.offsite_tick(DAY + timedelta(minutes=121)))
    assert len(bucket.runs()) == 1
    assert offsite.offsite_status(DAY + timedelta(minutes=122))["state"] == "ok"


def test_deleting_an_account_deletes_its_copies_and_makes_a_whole_run(instance):
    bucket, data, tmp = instance["bucket"], instance["data"], instance["tmp"]
    asyncio.run(offsite.offsite_tick(DAY))

    # What account deletion does: drop the registry row and the files.
    with sqlite3.connect(data / "virgil-central.db") as db:
        db.execute("DELETE FROM users WHERE db_filename = ?", (TWO,))
    (data / "users" / TWO).unlink()
    backup.delete_user_backups(TWO)
    assert offsite.offsite_status(DAY)["pending_deletions"] == 1

    later = DAY + timedelta(hours=2)
    asyncio.run(offsite.offsite_tick(later))
    assert not [k for k in bucket.objects if TWO in k]
    assert [k for k in bucket.objects if ONE in k]  # the other account's copies stay
    assert offsite.offsite_status(later)["pending_deletions"] == 0

    # The older run is now short a file, so a whole run is made straight away.
    newest = bucket.runs()[-1]
    assert len(bucket.runs()) == 2
    restored = tmp / "restored"
    unseal(bucket.download(f"daily/{newest}/", tmp / "download"), instance["identity"], restored)
    assert sorted(p.name for p in (restored / "users").iterdir()) == [ONE]


def test_a_deletion_waits_while_the_bucket_refuses(instance):
    bucket, data = instance["bucket"], instance["data"]
    asyncio.run(offsite.offsite_tick(DAY))
    with sqlite3.connect(data / "virgil-central.db") as db:
        db.execute("DELETE FROM users WHERE db_filename = ?", (TWO,))
    (data / "users" / TWO).unlink()
    offsite.queue_deletion(TWO)
    bucket.failure = "InternalError"
    asyncio.run(offsite.offsite_tick(DAY + timedelta(minutes=5)))
    assert offsite.offsite_status(DAY)["pending_deletions"] == 1

    bucket.failure = None
    asyncio.run(offsite.offsite_tick(DAY + timedelta(minutes=66)))
    assert not [k for k in bucket.objects if TWO in k]


def test_half_configured_reports_what_is_missing_and_does_nothing(instance, monkeypatch):
    monkeypatch.setattr(config, "OFFSITE_SECRET_ACCESS_KEY", "")
    with pytest.raises(offsite.OffsiteConfigError, match="VIRGIL_OFFSITE_SECRET_ACCESS_KEY"):
        offsite.offsite_settings()
    asyncio.run(offsite.offsite_tick(DAY))
    assert instance["bucket"].objects == {}
    assert offsite.offsite_status(DAY)["state"] == "misconfigured"

    monkeypatch.setattr(config, "OFFSITE_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(config, "OFFSITE_RECIPIENT", "age1notakey")
    with pytest.raises(offsite.OffsiteConfigError, match="not an age public key"):
        offsite.offsite_settings()


def test_off_by_default_queues_nothing(instance, monkeypatch):
    for attribute in ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "RECIPIENT"):
        monkeypatch.setattr(config, f"OFFSITE_{attribute}", "")
    assert offsite.offsite_settings() is None
    backup.delete_user_backups(TWO)
    assert not (backup.BACKUP_DIR / "offsite").exists()
    assert offsite.offsite_status()["state"] == "off"


def test_a_deletion_while_turned_off_waits_for_copies_to_return(instance, monkeypatch):
    bucket, data = instance["bucket"], instance["data"]
    asyncio.run(offsite.offsite_tick(DAY))
    names = ("ENDPOINT", "BUCKET", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "RECIPIENT")
    saved = {name: getattr(config, f"OFFSITE_{name}") for name in names}
    for name in names:  # turned off entirely
        monkeypatch.setattr(config, f"OFFSITE_{name}", "")
    with sqlite3.connect(data / "virgil-central.db") as db:
        db.execute("DELETE FROM users WHERE db_filename = ?", (TWO,))
    (data / "users" / TWO).unlink()
    backup.delete_user_backups(TWO)
    asyncio.run(offsite.offsite_tick(DAY + timedelta(hours=1)))
    assert [k for k in bucket.objects if TWO in k]

    for name, value in saved.items():
        monkeypatch.setattr(config, f"OFFSITE_{name}", value)
    asyncio.run(offsite.offsite_tick(DAY + timedelta(hours=2)))
    assert not [k for k in bucket.objects if TWO in k]


def test_a_run_clears_plain_copies_a_killed_run_left(instance):
    leftover = backup.BACKUP_DIR / ".offsite-killed" / "bundle" / "users"
    leftover.mkdir(parents=True)
    (leftover / ONE).write_bytes(b"plain database copy")
    asyncio.run(offsite.offsite_tick(DAY))
    assert not (backup.BACKUP_DIR / ".offsite-killed").exists()
    assert list(backup.BACKUP_DIR.glob(".offsite-*")) == []
