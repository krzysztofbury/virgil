"""Server settings an admin changes in the app, and the server's AI keys (#179).

A row in server_settings overrides the environment variable of the same name
until it is deleted. Secrets are stored encrypted with the instance key, like
each account's own AI keys. server_llm_providers mirrors an account's
llm_providers: the keys the server spends for accounts allowed to use them.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE server_settings (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_by TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute(
        """CREATE TABLE server_llm_providers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            api_key_enc TEXT NOT NULL,
            model TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )


async def validate(db: aiosqlite.Connection) -> None:
    for table, columns in {
        "server_settings": {"name", "value", "updated_at", "updated_by"},
        "server_llm_providers": {"id", "provider", "api_key_enc", "model", "is_active", "created_at"},
    }.items():
        found = {row["name"] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
        if found != columns:
            raise RuntimeError(f"Central {table} does not match migration 010")
