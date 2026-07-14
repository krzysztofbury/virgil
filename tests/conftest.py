"""Test fixtures. Env vars MUST be set before any `app.*` import —
app/config.py reads the environment at import time."""

import os
import re
import sqlite3
import tempfile
from pathlib import Path

# Idempotent: pytest may import this file twice (as `conftest` AND `tests.conftest`
# when tests mix import styles), which would re-run mkdtemp and repoint the env at a
# fresh empty dir mid-session — silently breaking any module that cached a path at
# import. The sentinel makes the FIRST import win and later ones reuse it.
if os.environ.get("_VIRGIL_TEST_TMP"):
    _TMP = os.environ["_VIRGIL_TEST_TMP"]
else:
    _TMP = tempfile.mkdtemp(prefix="virgil-test-")
    os.environ["_VIRGIL_TEST_TMP"] = _TMP
    os.environ["VIRGIL_CENTRAL_DB_PATH"] = f"{_TMP}/central.db"
    os.environ["VIRGIL_API_KEY"] = "test-key-123"
    os.environ["VIRGIL_ADMIN_EMAILS"] = "test@example.com"
    os.environ["VIRGIL_SECOND_BRAIN_PATH"] = ""
    # Hermetic tests: app/config.py setdefault-loads the developer's real .env,
    # which may carry a live LLM key or open registration — pin both here so a
    # test can never fire a real LLM call and the signup path always exercises
    # the closed-by-default + bootstrap-first-user flow.
    os.environ["VIRGIL_INTERNAL_LLM_KEY"] = ""
    os.environ["VIRGIL_REGISTRATION_OPEN"] = "false"
    os.environ["VIRGIL_API_SENSITIVE"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "test-password-123"

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


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def auth_client(client):
    """Client with a signed-up, onboarded admin user session."""
    token = csrf_token(client, "/signup")
    resp = client.post(
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
    return client
