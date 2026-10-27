"""Settings tells the user how old their newest backup is and whether the last
attempt failed, and the factory reset offers a download before it proceeds."""

import asyncio
import os
import re
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

import app.services.backup as backup_module
from app.migrations.runner import run_migrations
from tests.conftest import user_db_path

_NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)
_STEM = "22222222-2222-4222-8222-222222222222"
_OTHER_ACCOUNT = "11111111-1111-4111-8111-111111111111"


def _backup_file(directory, name: str, written_ago: timedelta, now: datetime = _NOW) -> None:
    path = directory / name
    path.write_bytes(b"")
    written = (now - written_ago).timestamp()
    os.utime(path, (written, written))


def _named(stem: str, created_ago: timedelta, job_id: int | None = None) -> str:
    suffix = f"-j{job_id:020d}" if job_id is not None else ""
    return f"{stem}-{(_NOW - created_ago).strftime('%Y-%m-%dT%H%M')}{suffix}.db"


def _at(ago: timedelta | None) -> str | None:
    return None if ago is None else (_NOW - ago).strftime("%Y-%m-%d %H:%M:%S.%f")


async def _insert_job(db, status: str, created_ago: timedelta, finished_ago: timedelta | None = None, error=""):
    created = _at(created_ago)
    await db.execute(
        """INSERT INTO jobs (kind, payload_json, status, idempotency_key, attempts, max_attempts,
                             retry_policy, run_after, last_error, created_at, started_at, finished_at, updated_at)
           VALUES ('backup', '{"trigger":"scheduled"}', ?, ?, 1, 3, 'automatic', ?, ?, ?, ?, ?, ?)""",
        (
            status,
            f"backup:test-{os.urandom(4).hex()}",
            created,
            error,
            created,
            None if status == "queued" else created,
            _at(finished_ago),
            created,
        ),
    )


def _status(tmp_path, monkeypatch, *, files=(), jobs=(), settings=()) -> dict:
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(backup_module, "BACKUP_DIR", backups)
    for name, written_ago in files:
        _backup_file(backups, name, written_ago)

    async def scenario():
        db = await aiosqlite.connect(tmp_path / f"{_STEM}.db")
        db.row_factory = aiosqlite.Row
        try:
            await run_migrations(db)
            for key, value in settings:
                await db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
            for job in jobs:
                await _insert_job(db, *job)
            await db.commit()
            return await backup_module.backup_status(db, now=_NOW)
        finally:
            await db.close()

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("files", "jobs", "settings", "state", "age"),
    [
        # Another account's backup is not this account's.
        ([(_named(_OTHER_ACCOUNT, _HOUR), _HOUR)], [], [], "none", None),
        ([(_named(_STEM, 3 * _HOUR), 3 * _HOUR)], [], [], "ok", "3 hours ago"),
        # A job queued three days ago that only ran a minute ago: the age is
        # when the copy was written, not when the job was queued.
        ([(_named(_STEM, 3 * _DAY, job_id=7), timedelta(minutes=1))], [], [], "ok", "1 minute ago"),
        # Daily schedule: just inside two intervals, then just past them.
        ([(_named(_STEM, 2 * _DAY), 2 * _DAY - timedelta(minutes=1))], [], [], "ok", "1 day ago"),
        ([(_named(_STEM, 2 * _DAY), 2 * _DAY + timedelta(minutes=1))], [], [], "overdue", "2 days ago"),
        # Off outranks a failure the scheduler will never retry while off.
        (
            [(_named(_STEM, 3 * _DAY), 3 * _DAY)],
            [("failed", 30 * _DAY, 30 * _DAY, "Disk full.")],
            [("backup_enabled", "0")],
            "off",
            "3 days ago",
        ),
        # A retried older job finished after a newer one failed: the last
        # attempt succeeded.
        (
            [(_named(_STEM, 5 * _HOUR, job_id=1), _HOUR)],
            [("succeeded", 5 * _HOUR, _HOUR), ("failed", 3 * _HOUR, 2 * _HOUR, "Disk full.")],
            [],
            "ok",
            "1 hour ago",
        ),
        ([(_named(_STEM, 3 * _HOUR), 3 * _HOUR)], [("queued", timedelta(minutes=5))], [], "running", "3 hours ago"),
        # A job queued for days hides nothing: the copy is still too old.
        ([(_named(_STEM, 10 * _DAY), 10 * _DAY)], [("queued", 9 * _DAY)], [], "overdue", "10 days ago"),
        ([], [("queued", timedelta(minutes=5))], [], "running", None),
        # An ambiguous job whose own file exists did make its copy.
        (
            [(_named(_STEM, 2 * _HOUR, job_id=1), 2 * _HOUR)],
            [("needs_attention", 2 * _HOUR, _HOUR)],
            [],
            "ok",
            "2 hours ago",
        ),
        # Intervals the Automation form never saves fall back to a day instead
        # of failing the page.
        ([(_named(_STEM, 30 * _HOUR), 30 * _HOUR)], [], [("backup_interval_hours", "nan")], "ok", "1 day ago"),
        ([(_named(_STEM, 30 * _HOUR), 30 * _HOUR)], [], [("backup_interval_hours", "1e300")], "ok", "1 day ago"),
        ([(_named(_STEM, 30 * _HOUR), 30 * _HOUR)], [], [("backup_interval_hours", "daily")], "ok", "1 day ago"),
    ],
    ids=[
        "other-account-only",
        "recent",
        "late-job-run",
        "inside-two-intervals",
        "past-two-intervals",
        "off-after-failure",
        "retry-finished-last",
        "queued",
        "queued-but-overdue",
        "first-backup-queued",
        "ambiguous-with-its-file",
        "nan-interval",
        "huge-interval",
        "text-interval",
    ],
)
def test_backup_status(tmp_path, monkeypatch, files, jobs, settings, state, age):
    status = _status(tmp_path, monkeypatch, files=files, jobs=jobs, settings=settings)

    assert (status["state"], status["latest_age"]) == (state, age)


@pytest.mark.parametrize("job_status", ["failed", "needs_attention"])
def test_an_attempt_that_failed_after_the_newest_copy_is_reported(tmp_path, monkeypatch, job_status):
    status = _status(
        tmp_path,
        monkeypatch,
        files=[(_named(_STEM, 26 * _HOUR), 26 * _HOUR)],
        jobs=[(job_status, 3 * _HOUR, 2 * _HOUR, "Disk full.")],
    )

    assert status["state"] == "failed"
    assert (status["failed_age"], status["failed_error"]) == ("2 hours ago", "Disk full.")
    assert status["latest_age"] == "1 day ago"


@pytest.fixture
def account_backups(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path)
    _backup_file(tmp_path, f"{user_db_path().stem}-2026-10-01T0000.db", 5 * _HOUR, now=datetime.now(UTC))
    return tmp_path


def test_data_tab_renders_the_status_line(auth_client, account_backups):
    html = auth_client.get("/settings?tab=data").text

    match = re.search(r'<p [^>]*data-backup-state="([a-z]+)"[^>]*>(.*?)</p>', html, re.S)
    assert match, "the Backups card has no status line"
    assert match[1] == "ok"
    assert " ".join(re.sub(r"<[^>]+>", " ", match[2]).split()).startswith("Latest backup: 5 hours ago (")


def test_reset_offers_a_download_before_the_reset_button(auth_client, account_backups):
    html = auth_client.get("/settings?tab=data").text

    offer = html.index("data-reset-backup")
    # The full export, because Restore takes it back after the reset.
    download = html.index('href="/settings/export/json"', offer)
    reset = html.index('action="/settings/factory-reset"')
    assert offer < download < reset
    assert "The newest server backup (5 hours ago) goes with the reset." in html[offer:reset]
