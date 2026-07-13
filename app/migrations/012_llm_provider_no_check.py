"""Rebuild llm_providers without the provider CHECK constraint.

The original schema allowed only ('claude','openai','gemini'), but migration 007
renames 'claude' to 'anthropic' and the settings UI offers Mistral, Groq, Ollama
and free-form providers — every one of those INSERT/UPDATEs violates the CHECK.
SQLite cannot drop a constraint, so the table is rebuilt (provider is a plain
LiteLLM string now; validation happens at the form layer).
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
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
