"""A per-user database exists exactly as long as its account does.

The file is never created by opening it, and deleting or resetting an account
removes its backups too.
"""

import os
import sqlite3

import pytest
from conftest import TEST_EMAIL, csrf_token


def _central_conn():
    return sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])


def test_a_missing_user_database_is_not_recreated(auth_client, client):
    """aiosqlite.connect creates a missing file. A request that read the account
    just before a factory reset or delete then opened the old name and left an
    empty, unmigrated database behind."""
    import asyncio

    from app.user_db import UserDatabaseMissing, open_user_db

    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    missing = "00000000-0000-4000-8000-000000000000.db"
    with pytest.raises(UserDatabaseMissing):
        asyncio.run(open_user_db(missing))
    assert not os.path.exists(os.path.join(users_dir, missing))

    central = _central_conn()
    current = central.execute("SELECT db_filename FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.execute("UPDATE users SET db_filename = ? WHERE email = ?", (missing, TEST_EMAIL))
    central.commit()
    try:
        # Gone for good, not swapped by a reset: say so instead of bouncing to /,
        # which opens the same missing file and would redirect to itself forever.
        for path in ("/daily", "/"):
            response = auth_client.get(path, follow_redirects=False)
            assert response.status_code == 503
        assert not os.path.exists(os.path.join(users_dir, missing))
    finally:
        central.execute("UPDATE users SET db_filename = ? WHERE email = ?", (current, TEST_EMAIL))
        central.commit()
        central.close()


def test_deleting_an_account_removes_its_database_and_backups(auth_client):
    import uuid

    from app.services.backup import BACKUP_DIR

    user_id = str(uuid.uuid4())
    users_dir = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users")
    (BACKUP_DIR / "pre-migration").mkdir(parents=True, exist_ok=True)
    files = [
        os.path.join(users_dir, f"{user_id}.db"),
        BACKUP_DIR / f"{user_id}-2026-10-01T0000.db",
        BACKUP_DIR / "pre-migration" / f"{user_id}-pre-migration-v036.db",
    ]
    kept = BACKUP_DIR / "22222222-2222-4222-8222-222222222222-2026-10-01T0000.db"
    for path in [*files, kept]:
        open(path, "wb").close()
    central = _central_conn()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, 'gone@example.com', 'x', ?)",
        (user_id, f"{user_id}.db"),
    )
    central.commit()
    try:
        token = csrf_token(auth_client, "/admin/users")
        response = auth_client.post(f"/admin/users/{user_id}/delete", data={"_csrf_token": token})
        assert response.status_code == 200

        assert [str(path) for path in files if os.path.exists(path)] == []
        assert kept.exists()
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()
        kept.unlink(missing_ok=True)


def test_startup_skips_a_missing_database_without_degrading_health(tmp_path, monkeypatch):
    """Startup used to create and migrate the missing file. Marking it a migration
    failure instead would hold /healthz at 503 and block every deploy for one account."""
    import asyncio

    from fastapi import FastAPI

    import app.central_db as central_db
    import app.services.scheduler as scheduler
    import app.user_db as user_db
    from app.main import _start_application

    async def no_op(*_args, **_kwargs):
        return None

    async def users():
        return [{"db_filename": "missing.db"}]

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    monkeypatch.setattr(central_db, "init_central_db", no_op)
    monkeypatch.setattr(central_db, "promote_admin_emails", no_op)
    monkeypatch.setattr(central_db, "get_all_users", users)
    monkeypatch.setattr(scheduler, "scheduler_loop", no_op)

    async def run():
        probe = FastAPI()
        task = await _start_application(probe)
        await task
        return probe.state.migration_failures

    assert asyncio.run(run()) == []
    assert not (tmp_path / "missing.db").exists()
