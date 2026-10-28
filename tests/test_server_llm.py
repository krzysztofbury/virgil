"""The server's own AI keys, managed like an account's (#179): the active one
serves accounts allowed to spend the server's key, ahead of the environment's
VIRGIL_INTERNAL_LLM_* and behind the account's own key."""

import sqlite3
import uuid
from pathlib import Path

import pytest
from conftest import _TMP

from app.services import server_llm


@pytest.fixture
def account(client, tmp_path, monkeypatch):
    """A migrated account database whose registry row may spend the server key."""
    import aiosqlite

    from app.migrations.runner import run_migrations

    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "")
    monkeypatch.setattr("app.config.INTERNAL_LLM_MODEL", "gemini/from-env")
    filename = f"llm-{uuid.uuid4()}.db"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename, allow_shared_llm) VALUES (?, ?, 'x', ?, 1)",
        (str(uuid.uuid4()), f"{filename}@example.com", filename),
    )
    central.commit()

    async def open_db():
        db = await aiosqlite.connect(tmp_path / filename)
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        return db

    db = client.portal.call(open_db)
    yield client.portal.call, db, central, filename
    client.portal.call(db.close)
    for provider in client.portal.call(server_llm.list_providers):
        client.portal.call(server_llm.delete_provider, provider["id"])
    central.execute("DELETE FROM users WHERE db_filename = ?", (filename,))
    central.commit()
    central.close()


def test_the_active_server_key_comes_before_the_environment(account, monkeypatch):
    from app.services.llm import llm_available, resolve_provider

    call, db, _central, _filename = account
    assert call(llm_available, db) is False

    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "env-key")
    assert call(resolve_provider, db) == ("gemini/from-env", "env-key")

    first = call(server_llm.add_provider, "xai", "xai/grok-4.7", "xai-key")
    second = call(server_llm.add_provider, "openai", "openai/gpt-5", "openai-key")
    assert call(resolve_provider, db) == ("gemini/from-env", "env-key"), "an added key is not in use yet"

    call(server_llm.activate_provider, first)
    assert call(resolve_provider, db) == ("xai/grok-4.7", "xai-key")
    call(server_llm.activate_provider, second)
    assert call(resolve_provider, db) == ("openai/gpt-5", "openai-key")

    call(server_llm.delete_provider, second)
    assert call(resolve_provider, db) == ("gemini/from-env", "env-key")


def test_the_server_key_needs_the_accounts_permission(account):
    from app.services.llm import llm_available, resolve_provider

    call, db, central, filename = account
    call(server_llm.activate_provider, call(server_llm.add_provider, "xai", "xai/grok-4.7", "xai-key"))
    assert call(llm_available, db) is True

    central.execute("UPDATE users SET allow_shared_llm = 0 WHERE db_filename = ?", (filename,))
    central.commit()
    assert call(llm_available, db) is False
    with pytest.raises(ValueError):
        call(resolve_provider, db)


def test_the_accounts_own_key_comes_first(account):
    from app.services.encryption import encrypt
    from app.services.llm import resolve_provider

    call, db, _central, _filename = account
    call(server_llm.activate_provider, call(server_llm.add_provider, "xai", "xai/grok-4.7", "xai-key"))

    async def own_key():
        await db.execute(
            "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES ('anthropic', ?, ?, 1)",
            (encrypt("own-key"), "anthropic/claude-sonnet-5"),
        )
        await db.commit()

    call(own_key)
    assert call(resolve_provider, db) == ("anthropic/claude-sonnet-5", "own-key")


def test_the_key_is_stored_encrypted_and_listed_masked(account):
    call = account[0]
    call(server_llm.add_provider, "xai", "xai/grok-4.7", "xai-secret-key-1234")
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        stored = central.execute("SELECT api_key_enc FROM server_llm_providers").fetchone()[0]
    assert "xai-secret" not in stored
    (listed,) = call(server_llm.list_providers)
    assert listed["api_key_masked"] == "...1234"
    assert "api_key_enc" not in listed


def test_an_undecryptable_server_key_counts_as_none(account):
    from app.services.llm import llm_available

    call, db, _central, _filename = account
    call(server_llm.activate_provider, call(server_llm.add_provider, "xai", "xai/grok-4.7", "xai-key"))
    with sqlite3.connect(Path(_TMP) / "central.db") as central:
        unreadable = "not-a-fernet-token"  # pragma: allowlist secret - synthetic fixture
        central.execute("UPDATE server_llm_providers SET api_key_enc = ?", (unreadable,))
    assert call(server_llm.server_model_and_key) is None
    assert call(llm_available, db) is False
    assert call(server_llm.active_model) == "xai/grok-4.7"
