"""Session-scoped durable-job status, polling, and explicit retry UI."""

import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import TEST_EMAIL, csrf_token, user_db_path

_NOW = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
_TERMINAL = {"succeeded", "failed", "cancelled", "needs_attention"}


def _clear_jobs(path: Path) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM sqlite_sequence WHERE name = 'jobs'")
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean_jobs(auth_client):
    path = user_db_path()
    _clear_jobs(path)
    yield
    _clear_jobs(path)


def _insert_job(
    path: Path,
    status: str,
    *,
    kind: str = "private_report",
    job_id: int | None = None,
    updated_at: str = _NOW,
) -> int:
    attempts = 0 if status == "queued" else 1
    started_at = None if status == "queued" else _NOW
    finished_at = _NOW if status in _TERMINAL else None
    locked_at = _NOW if status == "running" else None
    locked_by = "worker-test" if status == "running" else None
    claim_token = "a" * 32 if status == "running" else None
    last_error = "Safe public failure." if status in {"failed", "needs_attention"} else ""
    db = sqlite3.connect(path)
    try:
        cursor = db.execute(
            """INSERT INTO jobs
               (id, kind, payload_json, status, idempotency_key, attempts,
                max_attempts, retry_policy, run_after, locked_at, locked_by,
                claim_token, last_error, result_json, created_at, started_at,
                finished_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 3, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                kind,
                '{"private":"PAYLOAD_SECRET"}',
                status,
                f"IDEMPOTENCY_SECRET-{os.urandom(4).hex()}",
                attempts,
                _NOW,
                locked_at,
                locked_by,
                claim_token,
                last_error,
                '{"private":"RESULT_SECRET"}',
                _NOW,
                started_at,
                finished_at,
                updated_at,
            ),
        )
        db.commit()
        return int(cursor.lastrowid)
    finally:
        db.close()


def _job_status(path: Path, job_id: int) -> tuple[str, int, int]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT status, attempts, max_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        db.close()


def test_automation_lists_bounded_recent_jobs_without_sensitive_fields(auth_client):
    path = user_db_path()
    for index in range(9):
        _insert_job(path, "succeeded", kind=f"job_{index}")
    ambiguous_id = _insert_job(path, "needs_attention", kind="paid_briefing")

    response = auth_client.get("/settings?tab=automation")

    assert response.status_code == 200
    assert "Recent Jobs" in response.text
    assert response.text.count('data-job-id="') == 8
    assert f'data-job-id="{ambiguous_id}"' in response.text
    assert "Paid Briefing" in response.text
    assert "Needs review" in response.text
    assert "Check before retrying." in response.text
    assert "Retry anyway" in response.text
    assert "PAYLOAD_SECRET" not in response.text
    assert "RESULT_SECRET" not in response.text
    assert "IDEMPOTENCY_SECRET" not in response.text
    outer_form = response.text.index('<form method="POST" action="/settings/automation">')
    recent_jobs = response.text.index('id="recent-jobs-heading"')
    assert response.text.index("</form>", outer_form) < recent_jobs


@pytest.mark.parametrize(
    ("status", "label", "polling", "retry"),
    [
        ("queued", "Queued", True, False),
        ("running", "Running", True, False),
        ("succeeded", "Completed", False, False),
        ("failed", "Failed", False, True),
        ("cancelled", "Cancelled", False, False),
        ("needs_attention", "Needs review", False, True),
    ],
)
def test_status_partial_projects_each_state_accessibly(auth_client, status, label, polling, retry):
    job_id = _insert_job(user_db_path(), status)

    response = auth_client.get(f"/api/jobs/{job_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'role="status"' in response.text
    assert 'aria-live="polite"' in response.text
    assert f'data-job-status="{status}"' in response.text
    assert label in response.text
    assert ('hx-trigger="every 8s"' in response.text) is polling
    assert (f'action="/api/jobs/{job_id}/retry"' in response.text) is retry
    assert ('hx-sync="closest article:abort"' in response.text) is polling
    assert ('hx-sync="closest article:replace"' in response.text) is retry
    assert "PAYLOAD_SECRET" not in response.text
    assert "RESULT_SECRET" not in response.text
    assert "IDEMPOTENCY_SECRET" not in response.text


def test_poll_returns_204_until_updated_then_stops_for_terminal_state(auth_client):
    path = user_db_path()
    job_id = _insert_job(path, "queued")
    initial = auth_client.get(f"/api/jobs/{job_id}")
    version = re.search(r"known_version=([0-9a-f]{32})", initial.text)
    assert version

    unchanged = auth_client.get(f"/api/jobs/{job_id}?known_version={version.group(1)}")
    assert unchanged.status_code == 204
    assert unchanged.headers["cache-control"] == "no-store"
    assert unchanged.text == ""

    updated_at = (datetime.now(UTC) + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
    db = sqlite3.connect(path)
    try:
        db.execute(
            """UPDATE jobs SET status = 'succeeded', attempts = 1,
               started_at = ?, finished_at = ?, updated_at = ? WHERE id = ?""",
            (_NOW, updated_at, updated_at, job_id),
        )
        db.commit()
    finally:
        db.close()

    changed = auth_client.get(f"/api/jobs/{job_id}?known_version={version.group(1)}")
    assert changed.status_code == 200
    assert 'data-job-status="succeeded"' in changed.text
    assert 'hx-trigger="every 8s"' not in changed.text


def test_heartbeat_only_update_does_not_replace_visible_running_state(auth_client):
    path = user_db_path()
    job_id = _insert_job(path, "running")
    initial = auth_client.get(f"/api/jobs/{job_id}")
    version = re.search(r"known_version=([0-9a-f]{32})", initial.text)
    assert version

    heartbeat = (datetime.now(UTC) + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE jobs SET locked_at = ?, updated_at = ? WHERE id = ?", (heartbeat, heartbeat, job_id))
        db.commit()
    finally:
        db.close()

    unchanged = auth_client.get(f"/api/jobs/{job_id}?known_version={version.group(1)}")
    assert unchanged.status_code == 204
    assert unchanged.text == ""


def test_htmx_retry_is_atomic_and_second_submission_returns_current_partial(auth_client):
    path = user_db_path()
    job_id = _insert_job(path, "failed")
    token = csrf_token(auth_client, "/settings?tab=automation")
    request = {"data": {"_csrf_token": token}, "headers": {"HX-Request": "true"}}

    first = auth_client.post(f"/api/jobs/{job_id}/retry", **request)
    second = auth_client.post(f"/api/jobs/{job_id}/retry", **request)

    assert first.status_code == 200
    assert first.headers["X-Feedback-Kind"] == "success"
    assert first.headers["X-Feedback-Message"] == "Job queued for retry."
    assert 'data-job-status="queued"' in first.text
    assert second.status_code == 409
    assert second.headers["X-Feedback-Kind"] == "error"
    assert second.headers["X-Feedback-Swap"] == "true"
    assert 'data-job-status="queued"' in second.text
    assert _job_status(path, job_id) == ("queued", 1, 3)


def test_ambiguous_retry_requires_explicit_confirmation(auth_client):
    path = user_db_path()
    job_id = _insert_job(path, "needs_attention")
    token = csrf_token(auth_client, "/settings?tab=automation")
    headers = {"HX-Request": "true"}

    refused = auth_client.post(
        f"/api/jobs/{job_id}/retry",
        data={"_csrf_token": token},
        headers=headers,
    )
    confirmed = auth_client.post(
        f"/api/jobs/{job_id}/retry",
        data={"_csrf_token": token, "confirm_ambiguous": "yes"},
        headers=headers,
    )

    assert refused.status_code == 409
    assert "Confirm that you checked the external service" in refused.headers["X-Feedback-Message"]
    assert 'data-job-status="needs_attention"' in refused.text
    assert confirmed.status_code == 200
    assert 'data-job-status="queued"' in confirmed.text
    assert _job_status(path, job_id)[0] == "queued"


def test_retry_rejects_stale_failed_authorization_if_job_becomes_ambiguous(auth_client, monkeypatch):
    import app.routers.jobs as jobs_router
    from app.services.jobs import retry_job as real_retry_job

    path = user_db_path()
    job_id = _insert_job(path, "failed")

    async def interleaved_retry(db, target_job_id, expected_status, expected_attempts):
        await db.execute(
            "UPDATE jobs SET status = 'needs_attention', last_error = 'Outcome changed.' WHERE id = ?",
            (target_job_id,),
        )
        await db.commit()
        return await real_retry_job(db, target_job_id, expected_status, expected_attempts)

    monkeypatch.setattr(jobs_router, "retry_job", interleaved_retry)
    response = auth_client.post(
        f"/api/jobs/{job_id}/retry",
        data={"_csrf_token": csrf_token(auth_client, "/settings?tab=automation")},
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert 'data-job-status="needs_attention"' in response.text
    assert _job_status(path, job_id)[0] == "needs_attention"


def test_retry_rejects_aba_when_new_attempt_returns_to_needs_attention(auth_client, monkeypatch):
    import app.routers.jobs as jobs_router
    from app.services.jobs import retry_job as real_retry_job

    path = user_db_path()
    job_id = _insert_job(path, "needs_attention")

    async def interleaved_retry(db, target_job_id, expected_status, expected_attempts):
        await db.execute(
            "UPDATE jobs SET attempts = attempts + 1, last_error = 'New ambiguous attempt.' WHERE id = ?",
            (target_job_id,),
        )
        await db.commit()
        return await real_retry_job(db, target_job_id, expected_status, expected_attempts)

    monkeypatch.setattr(jobs_router, "retry_job", interleaved_retry)
    response = auth_client.post(
        f"/api/jobs/{job_id}/retry",
        data={
            "_csrf_token": csrf_token(auth_client, "/settings?tab=automation"),
            "confirm_ambiguous": "yes",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 409
    assert 'data-job-status="needs_attention"' in response.text
    assert _job_status(path, job_id) == ("needs_attention", 2, 3)


def test_native_retry_uses_visible_prg_feedback(auth_client):
    job_id = _insert_job(user_db_path(), "failed")
    response = auth_client.post(
        f"/api/jobs/{job_id}/retry",
        data={"_csrf_token": csrf_token(auth_client, "/settings?tab=automation")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    parsed = urlsplit(response.headers["location"])
    assert parsed.path == "/settings"
    assert parse_qs(parsed.query) == {"tab": ["automation"], "msg": ["Job queued for retry."]}


def test_retry_requires_csrf_and_rejects_nonretryable_state(auth_client):
    path = user_db_path()
    failed_id = _insert_job(path, "failed")
    running_id = _insert_job(path, "running")

    missing_csrf = auth_client.post(f"/api/jobs/{failed_id}/retry", data={}, headers={"HX-Request": "true"})
    conflict = auth_client.post(
        f"/api/jobs/{running_id}/retry",
        data={"_csrf_token": csrf_token(auth_client, "/settings?tab=automation")},
        headers={"HX-Request": "true"},
    )

    assert missing_csrf.status_code == 403
    assert _job_status(path, failed_id)[0] == "failed"
    assert conflict.status_code == 409
    assert 'data-job-status="running"' in conflict.text
    assert _job_status(path, running_id)[0] == "running"


def test_job_routes_are_session_only_and_missing_ids_are_not_cached(client, auth_client):
    for headers in ({}, {"X-API-Key": "test-key-123"}):
        response = client.get("/api/jobs/1", headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login")

    htmx = client.get("/api/jobs/1", headers={"HX-Request": "true"})
    assert htmx.status_code == 200
    assert htmx.headers["HX-Redirect"] == "/login"
    assert htmx.headers["cache-control"] == "no-store"
    assert htmx.text == ""

    missing = auth_client.get("/api/jobs/999999")
    negative = auth_client.get("/api/jobs/-1")
    assert (missing.status_code, negative.status_code) == (404, 404)
    assert missing.headers["cache-control"] == "no-store"


def test_same_numeric_job_id_resolves_inside_each_users_database(auth_client, monkeypatch):
    import app.routers.auth as auth_module

    first_path = user_db_path()
    _insert_job(first_path, "failed", kind="first_private", job_id=1)
    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", True)
    second = TestClient(app)
    users_dir = Path(os.environ["_VIRGIL_TEST_TMP"]) / "users"
    second_db_filename = None
    try:
        signup = second.post(
            "/signup",
            data={
                "email": "jobs-second@example.com",
                "password": "second-password-123",
                "password_confirm": "second-password-123",
                "_csrf_token": csrf_token(second, "/signup"),
            },
            follow_redirects=False,
        )
        assert signup.status_code == 303

        central = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
        try:
            second_db_filename = central.execute(
                "SELECT db_filename FROM users WHERE email = 'jobs-second@example.com'"
            ).fetchone()[0]
        finally:
            central.close()

        second_path = users_dir / second_db_filename
        second_db = sqlite3.connect(second_path)
        try:
            second_db.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('onboarding_completed', '1')")
            second_db.commit()
        finally:
            second_db.close()
        _insert_job(second_path, "failed", kind="second_private", job_id=1)

        first_response = auth_client.get("/api/jobs/1")
        second_response = second.get("/api/jobs/1")
        assert "First Private" in first_response.text
        assert "Second Private" not in first_response.text
        assert "Second Private" in second_response.text
        assert "First Private" not in second_response.text
    finally:
        second.close()
        central = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
        try:
            central.execute("DELETE FROM users WHERE email = 'jobs-second@example.com'")
            central.commit()
        finally:
            central.close()
        if second_db_filename:
            for suffix in ("", "-wal", "-shm"):
                path = Path(str(users_dir / second_db_filename) + suffix)
                if path.exists():
                    path.unlink()

    remaining = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
    try:
        assert remaining.execute("SELECT COUNT(*) FROM users WHERE email != ?", (TEST_EMAIL,)).fetchone()[0] == 0
    finally:
        remaining.close()
