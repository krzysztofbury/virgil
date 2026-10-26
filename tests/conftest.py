"""Test fixtures. Env vars MUST be set before any `app.*` import —
app/config.py reads the environment at import time."""

import atexit
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

# Provider capability metadata must not change an offline test's behavior.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

# Idempotent: pytest may import this file twice (as `conftest` AND `tests.conftest`
# when tests mix import styles), which would re-run mkdtemp and repoint the env at a
# fresh empty dir mid-session — silently breaking any module that cached a path at
# import. The sentinel makes the FIRST import win and later ones reuse it.
if os.environ.get("_VIRGIL_TEST_TMP"):
    _TMP = os.environ["_VIRGIL_TEST_TMP"]
else:
    _TMP = tempfile.mkdtemp(prefix="virgil-test-")
    os.environ["_VIRGIL_TEST_TMP"] = _TMP
    # Only the import that created the directory removes it, after the run.
    atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
    os.environ["VIRGIL_CENTRAL_DB_PATH"] = f"{_TMP}/central.db"
    os.environ["VIRGIL_API_KEY"] = "test-key-123"  # pragma: allowlist secret - synthetic fixture
    # The key serves this account only. An empty email must not pick an admin.
    os.environ["VIRGIL_API_USER_EMAIL"] = "test@example.com"
    os.environ["VIRGIL_ADMIN_EMAILS"] = "test@example.com"
    # Hermetic tests: app/config.py setdefault-loads the developer's real .env,
    # which may carry a live LLM key or open registration — pin both here so a
    # test can never fire a real LLM call and the signup path always exercises
    # the closed-by-default + bootstrap-first-user flow.
    os.environ["VIRGIL_INTERNAL_LLM_KEY"] = ""
    os.environ["VIRGIL_REGISTRATION_OPEN"] = "false"
    os.environ["VIRGIL_API_SENSITIVE"] = "false"
    # Enqueueing wakes a worker in the background. That is what production
    # wants and what makes queue state non-deterministic in a test, so it is
    # off here and turned on explicitly by the test that covers it.
    os.environ["VIRGIL_WORKER_WAKE"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.services.scheduler as scheduler  # noqa: E402
from app.main import app  # noqa: E402


async def _scheduler_idle() -> None:
    """The background loop, minus its ticks. Its first tick fires 60 s after the
    shared app starts, mid-suite now that the suite runs longer than that, and
    queued a scheduled backup into the tests' job counts. Tests that cover the
    scheduler call scheduler_tick themselves."""


scheduler.scheduler_loop = _scheduler_idle

TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "test-password-123"  # pragma: allowlist secret - synthetic fixture

_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def csrf_token(client: TestClient, path: str = "/signup") -> str:
    """GET a page and extract the CSRF token from its meta tag."""
    resp = client.get(path)
    match = _CSRF_META.search(resp.text)
    assert match, f"No CSRF meta tag on {path}"
    return match.group(1)


def user_db_path() -> Path:
    """Path to the single user DB file created by signup (for direct seeding in tests)."""
    db_files = list((Path(_TMP) / "users").glob("*.db"))
    assert len(db_files) == 1, f"Expected exactly one user DB, got {db_files}"
    return db_files[0]


def drain_jobs(worker_id: str = "test-worker") -> list:
    """Run every queued job for the test user, the way the scheduler would.

    Paid work left the request path, so a test that captures something and then
    reads the result has to let the worker run in between.
    """
    import asyncio

    import aiosqlite

    from app.services.job_worker import run_jobs_for_user

    path = user_db_path()

    async def opener(_filename):
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        return db

    async def closer(db):
        await db.close()

    async def scenario():
        control = await opener(path.name)
        ran = []
        try:
            # Batch after batch: the worker takes ten at a time, and jobs another
            # test left queued must not crowd out the one this test is waiting on.
            for _ in range(20):
                batch = await run_jobs_for_user(
                    control,
                    "test-user",
                    path.name,
                    worker_id=worker_id,
                    max_jobs=10,
                    handler_db_opener=opener,
                    handler_db_closer=closer,
                )
                if not batch.jobs:
                    return ran
                ran.extend(batch.jobs)
            raise AssertionError("drain_jobs: jobs still runnable after 20 batches")
        finally:
            await control.close()

    return asyncio.run(scenario())


def record_provider_calls(monkeypatch) -> list:
    """Every paid provider call goes through litellm.acompletion; record them.

    Patching the library itself, not a name a module imported, so an import
    alias cannot route a request around the check.
    """
    import litellm

    calls = []

    async def recorder(*args, **kwargs):
        calls.append(kwargs.get("model", ""))
        raise AssertionError("a request path called the provider")

    monkeypatch.setattr(litellm, "acompletion", recorder)
    return calls


def stat_value_for_label(html: str, label: str) -> float:
    """Numeric value of a "kg" KPI or grouped personal best.

    Personal bests expose their exercise and heaviest load as data attributes;
    KPIs retain the stat-value/stat-label structure. Both paths read the actual
    rendered page rather than a hand-copied aggregate query against sqlite.
    """
    pb_pattern = re.compile(r'data-pb-exercise="' + re.escape(label) + r'"\s+data-pb-best="([\d,]+(?:\.\d+)?)"')
    pb_match = pb_pattern.search(html)
    if pb_match:
        return float(pb_match.group(1).replace(",", ""))

    pattern = re.compile(
        r'stat-value">([\d,]+(?:\.\d+)?)\s*<small[^>]*>kg</small></div>\s*<div class="stat-label">'
        + re.escape(label)
        + r"</div>"
    )
    match = pattern.search(html)
    assert match, f"no stat-card found for label {label!r}"
    return float(match.group(1).replace(",", ""))


def plain_stat_value_for_label(html: str, label: str) -> float:
    """Same, for a stat-card whose value carries no unit <small> (Sessions, Total Reps).

    The "kg" anchor above cannot read those, which is part of why the Total Reps
    and Sessions KPIs had no assertion anywhere in the suite — dropping their
    query filters entirely left everything green.
    """
    pattern = re.compile(
        r'stat-value">([\d,]+(?:\.\d+)?)\s*</div>\s*<div class="stat-label">' + re.escape(label) + r"</div>"
    )
    match = pattern.search(html)
    assert match, f"no plain stat-card found for label {label!r}"
    return float(match.group(1).replace(",", ""))


def _complete_onboarding() -> None:
    """Mark onboarding as done directly in the (single) user DB file."""
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('onboarding_completed', '1')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Every TestClient request shares one client IP, so the per-IP auth limit
    (10/min) trips across unrelated tests — reset the buckets between tests."""
    import app.rate_limit as rate_limit

    rate_limit._buckets.clear()
    yield


@pytest.fixture
def sent_mail(monkeypatch) -> list:
    """Turn outgoing email on and collect each message instead of sending it."""
    from app import config
    from app.services import mailer

    sent: list = []
    monkeypatch.setattr(config, "MAIL_BACKEND", "log")
    monkeypatch.setattr(mailer, "schedule", sent.append)

    async def run_now(work):
        # Deferred work runs inside the request here, so a test sees its mail.
        await work

    monkeypatch.setattr(mailer, "defer", run_now)
    return sent


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_client(client):
    """Client with a signed-up, onboarded admin user session."""
    # `client` owns the one application lifespan for this test session. Use a
    # second cookie jar against that running app so login state cannot leak into
    # tests that intentionally exercise anonymous requests.
    authenticated = TestClient(app)
    assert client.portal is not None, "the session client must own the application lifespan"
    authenticated.portal = client.portal
    try:
        token = csrf_token(authenticated, "/signup")
        resp = authenticated.post(
            "/signup",
            data={
                "email": TEST_EMAIL,
                "display_name": "Test",
                "password": TEST_PASSWORD,
                "password_confirm": TEST_PASSWORD,
                "_csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, f"Signup failed: {resp.status_code} {resp.text[:200]}"
        _complete_onboarding()
        yield authenticated
    finally:
        authenticated.close()


def track_porn() -> int:
    """Make Porn the first tracked Freedom habit and return its id.

    The REST API's Freedom calls without ?habit= act on the first tracked habit,
    which is what an account migrated from the No Porn module has.
    """
    from app.services.habits import PRESETS_BY_SLUG

    # The catalog's own Porn habit, so a test that reads its labels or slip
    # definition gets the same row whichever test happened to create it first.
    porn = PRESETS_BY_SLUG["porn"]
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "INSERT OR IGNORE INTO habits (slug, name, slip_label, slip_definition, amount_unit, slip_threshold, "
            "near_miss_label, start_date) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01')",
            tuple(
                porn[key]
                for key in (
                    "slug",
                    "name",
                    "slip_label",
                    "slip_definition",
                    "amount_unit",
                    "slip_threshold",
                    "near_miss_label",
                )
            ),
        )
        conn.execute("UPDATE habits SET tracked = 1, position = -1 WHERE slug = 'porn'")
        conn.commit()
        return conn.execute("SELECT id FROM habits WHERE slug = 'porn'").fetchone()[0]
    finally:
        conn.close()
