"""Settings tells the user how old their newest backup is and whether the last
attempt failed, and the factory reset offers a download before it proceeds."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

import app.services.backup as backup_module
from tests.conftest import user_db_path

_OTHER_ACCOUNT = "11111111-1111-4111-8111-111111111111"


def _backup_file(directory, stem: str, age: timedelta, *, job_suffix: bool = False) -> None:
    taken = (datetime.now(UTC) - age).strftime("%Y-%m-%dT%H%M")
    suffix = f"-j{1:020d}" if job_suffix else ""
    (directory / f"{stem}-{taken}{suffix}.db").write_bytes(b"")


def _backup_job(status: str, age: timedelta) -> None:
    at = (datetime.now(UTC) - age).strftime("%Y-%m-%d %H:%M:%S")
    started = None if status == "queued" else at
    finished = at if status in {"succeeded", "failed", "needs_attention"} else None
    db = sqlite3.connect(user_db_path())
    try:
        db.execute(
            """INSERT INTO jobs (kind, payload_json, status, idempotency_key, attempts, max_attempts,
                                 retry_policy, run_after, last_error, created_at, started_at, finished_at, updated_at)
               VALUES ('backup', '{"trigger":"scheduled"}', ?, ?, 1, 3, 'automatic', ?, '', ?, ?, ?, ?)""",
            (status, f"backup:test-{status}", at, at, started, finished, at),
        )
        db.commit()
    finally:
        db.close()


def _set(key: str, value: str) -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def backups(auth_client, tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module, "BACKUP_DIR", tmp_path)
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.commit()
    finally:
        db.close()
    yield tmp_path
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.commit()
    finally:
        db.close()
    _set("backup_enabled", "1")
    _set("backup_interval_hours", "24")


def _status_line(html: str) -> tuple[str, str]:
    match = re.search(r'<p data-backup-state="([a-z]+)"[^>]*>(.*?)</p>', html, re.S)
    assert match, "the Automated Backup card has no status line"
    return match[1], " ".join(re.sub(r"<[^>]+>", " ", match[2]).split())


@pytest.mark.parametrize(
    ("files", "job", "enabled", "state", "text"),
    [
        # Another account's backup is not this account's.
        ([(_OTHER_ACCOUNT, timedelta(hours=1))], None, "1", "none", "None yet No backup has been made yet."),
        ([("own", timedelta(hours=3))], None, "1", "ok", "OK Latest backup: 3 hours ago"),
        # A job-made copy carries a suffix after the time; the newest copy wins.
        (
            [("own", timedelta(days=5)), ("own-job", timedelta(minutes=1))],
            None,
            "1",
            "ok",
            "OK Latest backup: 1 minute ago",
        ),
        # Daily schedule, nothing for three days: two missed runs.
        ([("own", timedelta(days=3))], None, "1", "overdue", "Overdue No backup in more than two intervals."),
        (
            [("own", timedelta(days=3))],
            None,
            "0",
            "off",
            "Off Automated backup is off. Latest backup: 3 days ago",
        ),
        # The file cannot show a failure; the newest job can.
        (
            [("own", timedelta(hours=26))],
            ("failed", timedelta(hours=2)),
            "1",
            "failed",
            "Failed The last backup attempt failed 2 hours ago. Recent Jobs below has the error. "
            "Latest backup: 1 day ago",
        ),
        ([("own", timedelta(hours=3))], ("queued", timedelta(0)), "1", "running", "Running"),
    ],
    ids=["other-account-only", "recent", "newest-of-two", "overdue", "off", "failed-after-copy", "running"],
)
def test_automation_tab_shows_the_latest_backup_age_and_status(auth_client, backups, files, job, enabled, state, text):
    stem = user_db_path().stem
    for owner, age in files:
        _backup_file(
            backups,
            stem if owner.startswith("own") else owner,
            age,
            job_suffix=owner == "own-job",
        )
    if job:
        _backup_job(*job)
    _set("backup_enabled", enabled)

    actual_state, actual_text = _status_line(auth_client.get("/settings?tab=automation").text)

    assert actual_state == state
    assert actual_text.startswith(text)


def test_reset_offers_a_download_before_the_reset_button(auth_client, backups):
    _backup_file(backups, user_db_path().stem, timedelta(hours=5))

    html = auth_client.get("/settings?tab=security").text

    offer = html.index("data-reset-backup")
    download = html.index('href="/settings/backup"', offer)
    reset = html.index('action="/settings/factory-reset"')
    assert offer < download < reset
    assert "The newest server backup (5 hours ago) goes with the reset." in html[offer:reset]
