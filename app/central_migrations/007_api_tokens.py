"""Personal API tokens: each account's own credential for the REST API and /mcp.

They live in the registry because a request is authenticated before any
account's database is opened, and a hash lookup cannot scan every file.
"""

import aiosqlite

_COLUMNS = [
    ("id", "TEXT", 0, None, 1),
    ("user_id", "TEXT", 1, None, 0),
    ("name", "TEXT", 1, None, 0),
    ("token_hash", "TEXT", 1, None, 0),
    ("token_prefix", "TEXT", 1, None, 0),
    ("scopes", "TEXT", 1, None, 0),
    ("expires_at", "TEXT", 1, "''", 0),
    ("last_used_at", "TEXT", 1, "''", 0),
    ("revoked_at", "TEXT", 1, "''", 0),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
]


async def up(db: aiosqlite.Connection) -> None:
    # Only the SHA-256 of a token is stored. The secrets are 256 random bits, so a
    # slow hash buys nothing and would rule out the unique index the lookup needs.
    await db.execute(
        """CREATE TABLE api_tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_prefix TEXT NOT NULL,
            scopes TEXT NOT NULL,
            expires_at TEXT NOT NULL DEFAULT '',
            last_used_at TEXT NOT NULL DEFAULT '',
            revoked_at TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    await db.execute("CREATE INDEX idx_api_tokens_user ON api_tokens(user_id)")


async def validate(db: aiosqlite.Connection) -> None:
    columns = await db.execute_fetchall("PRAGMA table_info(api_tokens)")
    actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual != _COLUMNS:
        raise RuntimeError("Central api_tokens does not match migration 007")
    keys = await db.execute_fetchall("PRAGMA foreign_key_list(api_tokens)")
    if [(row["from"], row["table"], row["to"], row["on_delete"]) for row in keys] != [
        ("user_id", "users", "id", "CASCADE")
    ]:
        raise RuntimeError("Central api_tokens.user_id must cascade from users")
    indexes = {row["name"] for row in await db.execute_fetchall("PRAGMA index_list(api_tokens)")}
    if "idx_api_tokens_user" not in indexes:
        raise RuntimeError("Central api_tokens is missing its user index")
