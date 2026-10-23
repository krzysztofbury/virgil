"""llm_available() must honor the internal env-var fallback, not just DB providers."""

import asyncio

import pytest


async def _fresh_db(tmp_path, filename: str = "llm.db"):
    import aiosqlite

    from app.migrations.runner import run_migrations

    db = await aiosqlite.connect(tmp_path / filename)
    db.row_factory = aiosqlite.Row
    await run_migrations(db)
    return db


@pytest.mark.parametrize(
    ("account", "expected"),
    [("allowed", True), ("withdrawn", False), ("none", False)],
    ids=["account-allowed", "account-withdrawn", "no-account"],
)
def test_the_server_key_serves_only_accounts_allowed_to_spend_it(client, tmp_path, monkeypatch, account, expected):
    """The internal key alone made every account's AI available (the old regression was the
    opposite: a DB provider row was required). An invited account must not bill the operator.
    `client` runs the app's lifespan, which opens the central registry and closes it again."""
    import sqlite3
    import uuid
    from pathlib import Path

    from conftest import _TMP

    from app.services.llm import llm_available

    filename = f"llm-{uuid.uuid4()}.db"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    if account != "none":
        central.execute(
            "INSERT INTO users (id, email, password_hash, db_filename, allow_shared_llm) VALUES (?, ?, 'x', ?, ?)",
            (str(uuid.uuid4()), f"{filename}@example.com", filename, int(account == "allowed")),
        )
        central.commit()

    async def scenario():
        db = await _fresh_db(tmp_path, filename)
        try:
            monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "")
            without_key = await llm_available(db)
            monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "internal-key")
            return without_key, await llm_available(db)
        finally:
            await db.close()

    try:
        assert asyncio.run(scenario()) == (False, expected)
    finally:
        central.execute("DELETE FROM users WHERE db_filename = ?", (filename,))
        central.commit()
        central.close()


def test_llm_available_via_db_provider(tmp_path, monkeypatch):
    from app.services.encryption import encrypt
    from app.services.llm import llm_available

    async def scenario():
        db = await _fresh_db(tmp_path)
        monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "")
        await db.execute(
            "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES ('anthropic', ?, 'anthropic/claude-sonnet-5', 1)",
            (encrypt("sk-test"),),
        )
        await db.commit()
        available = await llm_available(db)
        await db.close()
        return available

    assert asyncio.run(scenario()) is True


@pytest.mark.parametrize("path", ["onboarding", "medical_text", "medical_pdf"])
def test_onboarding_and_medical_import_use_the_accounts_key_not_the_servers(client, tmp_path, monkeypatch, path):
    """Both used to read VIRGIL_INTERNAL_LLM_KEY directly, whoever the account was."""
    import sqlite3
    import uuid
    from pathlib import Path

    import litellm
    from conftest import _TMP

    from app.services import medical_import, onboarding
    from app.services.encryption import encrypt

    filename = f"paths-{uuid.uuid4()}.db"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename, allow_shared_llm) VALUES (?, ?, 'x', ?, 0)",
        (str(uuid.uuid4()), f"{filename}@example.com", filename),
    )
    central.commit()
    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "server-key")
    sent = []

    async def fake_acompletion(**kwargs):
        sent.append((kwargs["model"], kwargs["api_key"]))
        raise litellm.Timeout("stop here", kwargs["model"], "test")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    call = {
        "onboarding": lambda db: onboarding._llm_call(db, "system", "user"),
        "medical_text": lambda db: medical_import.parse_medical_text(db, "HGB 15.8"),
        "medical_pdf": lambda db: medical_import.extract_pdf_markers(db, b"%PDF-1.4"),
    }[path]

    async def scenario():
        db = await _fresh_db(tmp_path, filename)
        try:
            with pytest.raises(ValueError, match="add your own API key"):
                await call(db)
            assert sent == [], "no paid call without a key the account may use"
            await db.execute(
                "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES ('anthropic', ?, 'anthropic/own-model', 1)",
                (encrypt("own-key"),),
            )
            await db.commit()
            with pytest.raises(Exception):  # noqa: B017 - the fake transport stops every call
                await call(db)
            assert sent == [("anthropic/own-model", "own-key")]
        finally:
            await db.close()

    try:
        asyncio.run(scenario())
    finally:
        central.execute("DELETE FROM users WHERE db_filename = ?", (filename,))
        central.commit()
        central.close()
