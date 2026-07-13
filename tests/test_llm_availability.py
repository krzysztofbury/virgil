"""llm_available() must honor the internal env-var fallback, not just DB providers."""

import asyncio


async def _fresh_db(tmp_path):
    import aiosqlite

    from app.migrations.runner import run_migrations

    db = await aiosqlite.connect(tmp_path / "llm.db")
    db.row_factory = aiosqlite.Row
    await run_migrations(db)
    return db


def test_llm_available_via_internal_key(tmp_path, monkeypatch):
    from app.services.llm import llm_available

    async def scenario():
        db = await _fresh_db(tmp_path)
        # No DB provider, no internal key → unavailable.
        monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "")
        none_available = await llm_available(db)

        # Internal env key alone → available (this was the regression: UI hid
        # AI features unless a DB provider row existed).
        monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "internal-key")
        internal_available = await llm_available(db)
        await db.close()
        return none_available, internal_available

    none_available, internal_available = asyncio.run(scenario())
    assert none_available is False
    assert internal_available is True


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
