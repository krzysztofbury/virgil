"""Single-use invites: how someone outside the household gets an account while registration stays closed."""

import aiosqlite

_COLUMNS = [
    ("id", "TEXT", 0, None, 1),
    ("token_hash", "TEXT", 1, None, 0),
    ("email", "TEXT", 1, "''", 0),
    ("role", "TEXT", 1, "'user'", 0),
    ("allow_shared_llm", "INTEGER", 1, "0", 0),
    ("created_by", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
    ("expires_at", "TEXT", 1, None, 0),
    ("used_at", "TEXT", 1, "''", 0),
    ("used_by", "TEXT", 1, "''", 0),
]


async def up(db: aiosqlite.Connection) -> None:
    # Only the SHA-256 of the link's secret is stored. Deleting the admin who
    # issued an invite deletes it too: an outstanding link should not outlive
    # the account that vouched for it.
    await db.execute(
        """CREATE TABLE invites (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
            allow_shared_llm INTEGER NOT NULL DEFAULT 0 CHECK(allow_shared_llm IN (0, 1)),
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT '',
            used_by TEXT NOT NULL DEFAULT ''
        )"""
    )


async def validate(db: aiosqlite.Connection) -> None:
    columns = await db.execute_fetchall("PRAGMA table_info(invites)")
    actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual != _COLUMNS:
        raise RuntimeError("Central invites does not match migration 005")
    keys = await db.execute_fetchall("PRAGMA foreign_key_list(invites)")
    if [(row["from"], row["table"], row["to"], row["on_delete"]) for row in keys] != [
        ("created_by", "users", "id", "CASCADE")
    ]:
        raise RuntimeError("Central invites.created_by must cascade from users")
