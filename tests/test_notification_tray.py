"""Feedback and job notifications live in one fixed top-right tray."""

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.conftest import user_db_path

_NOW = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


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


def _insert_job(path: Path, status: str, *, kind: str = "oura_sync") -> int:
    finished_at = _NOW if status in {"succeeded", "failed", "needs_attention", "cancelled"} else None
    running = status == "running"
    db = sqlite3.connect(path)
    try:
        cursor = db.execute(
            """INSERT INTO jobs
               (kind, payload_json, status, idempotency_key, attempts, max_attempts,
                retry_policy, run_after, locked_at, locked_by, claim_token,
                last_error, result_json, created_at, started_at, finished_at, updated_at)
               VALUES (?, '{}', ?, ?, 1, 3, 'manual', ?, ?, ?, ?, '', '{}', ?, ?, ?, ?)""",
            (
                kind,
                status,
                f"tray-{status}-{kind}",
                _NOW,
                _NOW if running else None,
                "worker-test" if running else None,
                "a" * 32 if running else None,
                _NOW,
                _NOW,
                finished_at,
                _NOW,
            ),
        )
        db.commit()
        return int(cursor.lastrowid)
    finally:
        db.close()


def test_tray_is_a_fixed_overlay_outside_the_main_flow(auth_client):
    response = auth_client.get("/oura")

    assert response.status_code == 200
    tray = response.text.index('id="notification-tray"')
    main = response.text.index('<main class="container">')
    assert tray < main, "the tray must not sit inside the scrolling content column"
    assert 'id="job-notifications"' in response.text


def test_tray_css_pins_the_top_right_corner_and_keeps_empty_regions_collapsed():
    css = Path("app/static/css/app.css").read_text()
    rule = css[css.index(".notification-tray {") :]
    rule = rule[: rule.index("}")]

    assert "position: fixed" in rule
    assert "right: 1rem" in rule
    assert "top: calc(var(--nav-height)" in rule
    assert "pointer-events: none" in rule, "an empty tray must not swallow clicks in the page corner"
    assert ".notification-tray > section:empty { display: none; }" in css
    assert "prefers-reduced-motion" in css[css.index("notification-enter") :]


def test_success_feedback_self_dismisses_but_an_error_waits_for_the_user(auth_client):
    success = auth_client.get("/oura?msg=Signed+in")
    failure = auth_client.get("/oura?err=Nope")

    assert "data-feedback-autodismiss" in success.text
    assert "Signed in" in success.text
    error_box = failure.text[failure.text.index('id="feedback-error"') :]
    error_box = error_box[: error_box.index("</section>")]
    assert "data-feedback-autodismiss" not in error_box
    assert "data-feedback-dismiss" in error_box


def test_shared_js_dismisses_only_success_and_terminal_job_notifications():
    js = Path("app/static/js/app.js").read_text()

    assert "TOAST_DISMISS_MS = 4000" in js
    assert "'#job-notifications [data-job-status=\"succeeded\"]'" in js
    assert 'data-job-status="failed"' not in js, "a failure must stay until it is read"
    scan = js[js.index("function armAutoDismiss") : js.index("function scanNotifications")]
    assert "mouseenter" in scan and "focusin" in scan, "hover and focus must hold the timer open"


@pytest.mark.parametrize("status", ["running", "succeeded", "needs_attention"])
def test_a_queued_job_reports_in_the_tray_not_as_a_page_section(auth_client, status):
    job_id = _insert_job(user_db_path(), status)

    response = auth_client.get(f"/oura?job_id={job_id}")

    assert response.status_code == 200
    tray = response.text[response.text.index('id="job-notifications"') :]
    tray = tray[: tray.index("</section>")]
    assert f'data-job-id="{job_id}"' in tray
    assert "data-job-dismiss" in tray
    assert response.text.count(f'data-job-id="{job_id}"') == 1, "the card must not also render inline"
    assert 'class="card job-activity-card" aria-label' not in response.text


def test_enqueue_redirect_no_longer_scrolls_to_a_now_fixed_card(auth_client):
    routers = Path("app/routers")
    sources = "\n".join(path.read_text() for path in routers.glob("*.py"))

    assert not re.search(r"#job-status-\{", sources), "a fixed tray card has nothing to scroll to"


def test_every_job_kind_reports_under_the_name_the_user_knows(auth_client):
    """Title-casing the kind read as "Wod Parse" and "Andy Generation"."""
    from app.routers.jobs import _KIND_LABELS
    from app.services.job_worker import JOB_HANDLERS

    assert set(JOB_HANDLERS) <= set(_KIND_LABELS), "a registered kind with no label falls back to its slug"
    job_id = _insert_job(user_db_path(), "queued", kind="wod_parse")
    response = auth_client.get(f"/oura?job_id={job_id}")
    assert "Training note analysis" in response.text
    assert "Wod Parse" not in response.text
