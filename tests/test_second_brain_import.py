"""The Second Brain folder is one server-wide directory of the install owner's notes.

Only the owner (the oldest account) may import it; any other account would copy
the owner's journal, blood results and goals into its own database.
"""

import os
import sqlite3

import pytest
from conftest import csrf_token
from fastapi.testclient import TestClient


@pytest.fixture
def second_account(auth_client, monkeypatch):
    import app.routers.auth as auth_module
    from app.main import app

    monkeypatch.setattr(auth_module, "REGISTRATION_OPEN", True)
    other = TestClient(app)
    password = "second-password-123"  # pragma: allowlist secret - synthetic fixture
    resp = other.post(
        "/signup",
        data={
            "email": "second@example.com",
            "password": password,
            "password_confirm": password,
            "_csrf_token": csrf_token(other, "/signup"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    central = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
    db_file = central.execute("SELECT db_filename FROM users WHERE email = 'second@example.com'").fetchone()[0]
    central.close()
    path = os.path.join(os.environ["_VIRGIL_TEST_TMP"], "users", db_file)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('onboarding_completed', '1')")
    try:
        yield other
    finally:
        central = sqlite3.connect(os.environ["VIRGIL_CENTRAL_DB_PATH"])
        central.execute("DELETE FROM users WHERE email = 'second@example.com'")
        central.commit()
        central.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def test_only_the_install_owner_can_import_the_second_brain(auth_client, second_account, monkeypatch):
    imported = []

    async def fake_import_all(db):
        imported.append(db)

    monkeypatch.setattr("app.services.markdown_import.import_all", fake_import_all)

    page = second_account.get("/settings?tab=data")
    assert page.status_code == 200
    assert 'action="/settings/import"' not in page.text
    refused = second_account.post(
        "/settings/import",
        data={"_csrf_token": csrf_token(second_account, "/settings?tab=data")},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert imported == []

    assert 'action="/settings/import"' in auth_client.get("/settings?tab=data").text
    owner = auth_client.post(
        "/settings/import", data={"_csrf_token": csrf_token(auth_client, "/settings?tab=data")}, follow_redirects=False
    )
    assert owner.status_code == 303
    assert len(imported) == 1
