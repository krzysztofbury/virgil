"""Convert existing llm_providers rows to LiteLLM model string format.

Old format: provider='claude', model='claude-sonnet-4-20250514'
New format: provider='anthropic', model='anthropic/claude-sonnet-4-20250514'

The original schema constrained provider to ('claude','openai','gemini') — the
claude→anthropic rename below violates that CHECK on legacy databases, so the
table is rebuilt without it first (SQLite cannot drop a constraint). Migration
012 repeats the rebuild for databases that ran an older version of this file.
"""

import aiosqlite

PROVIDER_MAP = {
    "claude": ("anthropic", "anthropic/"),
    "openai": ("openai", "openai/"),
    "gemini": ("gemini", "gemini/"),
}


async def _rebuild_without_provider_check(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS llm_providers_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            api_key_enc TEXT NOT NULL,
            model TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )"""
    )
    await db.execute(
        """INSERT INTO llm_providers_new (id, provider, api_key_enc, model, is_active, created_at)
           SELECT id, provider, api_key_enc, model, is_active, created_at FROM llm_providers"""
    )
    await db.execute("DROP TABLE llm_providers")
    await db.execute("ALTER TABLE llm_providers_new RENAME TO llm_providers")


async def up(db: aiosqlite.Connection) -> None:
    await _rebuild_without_provider_check(db)

    rows = await db.execute_fetchall("SELECT id, provider, model FROM llm_providers")
    for row in rows:
        old_provider = row["provider"]
        old_model = row["model"]
        if old_provider in PROVIDER_MAP:
            new_provider, prefix = PROVIDER_MAP[old_provider]
            # Only prefix if not already in LiteLLM format.
            new_model = f"{prefix}{old_model}" if not old_model.startswith(prefix) else old_model
            await db.execute(
                "UPDATE llm_providers SET provider = ?, model = ? WHERE id = ?",
                (new_provider, new_model, row["id"]),
            )
