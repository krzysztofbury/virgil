"""Upgrade path from a real pre-007 database.

Regression: the original llm_providers table has CHECK(provider IN
('claude','openai','gemini')); migration 007 renames claude→anthropic, which
violated that CHECK and bricked the whole migration chain for any legacy user
with a Claude provider configured.
"""

import asyncio
import importlib


async def _legacy_pre007_db(tmp_path):
    """Build a DB exactly as it looked after migrations 001–006, with the
    ORIGINAL CHECK constraint and a legacy claude provider row."""
    import aiosqlite

    db = await aiosqlite.connect(tmp_path / "legacy.db")
    db.row_factory = aiosqlite.Row

    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    for version, name in [
        (1, "001_initial_schema.py"),
        (2, "002_migrate_llm_api_keys.py"),
        (3, "003_experiment_sources.py"),
        (4, "004_add_webhook_columns.py"),
        (5, "005_feature_flags.py"),
        (6, "006_training_overhaul.py"),
    ]:
        mod = importlib.import_module(f"app.migrations.{name[:-3]}")
        await mod.up(db)
        await db.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)", (version, name))
        await db.commit()

    # Recreate llm_providers with the ORIGINAL CHECK (the live SCHEMA constant
    # no longer carries it) and seed a legacy claude row.
    await db.execute("DROP TABLE llm_providers")
    await db.execute(
        """CREATE TABLE llm_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL CHECK(provider IN ('claude','openai','gemini')),
            api_key_enc TEXT NOT NULL,
            model TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) "
        "VALUES ('claude', 'enc', 'claude-sonnet-4-20250514', 1)"
    )
    await db.commit()
    return db


def test_pre007_db_with_claude_provider_upgrades_cleanly(tmp_path):
    async def scenario():
        from app.migrations.runner import run_migrations

        db = await _legacy_pre007_db(tmp_path)
        # Must run 007..013 without tripping the legacy CHECK.
        await run_migrations(db)

        provider = dict((await db.execute_fetchall("SELECT * FROM llm_providers"))[0])
        versions = [r["version"] for r in await db.execute_fetchall("SELECT version FROM schema_migrations")]

        # And the constraint must be gone: LiteLLM provider names now insert.
        await db.execute(
            "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES ('mistral', 'e', 'm', 0)"
        )
        await db.commit()
        await db.close()
        return provider, versions

    provider, versions = asyncio.run(scenario())
    assert provider["provider"] == "anthropic"
    assert provider["model"] == "anthropic/claude-sonnet-4-20250514"
    assert provider["is_active"] == 1
    assert max(versions) >= 13, "Migration chain must complete past 007"
