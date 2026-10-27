"""Emailed sign-in links and MFA recovery codes (#168).

Both are one-time secrets: only the SHA-256 of each is stored. A sign-in link
either signs an account in or, for a signup, creates the account once its
address is proven; a signup link has no account yet, so it carries what the
account will be created with.
"""

import aiosqlite

_LINK_COLUMNS = [
    ("id", "TEXT", 0, None, 1),
    ("purpose", "TEXT", 1, None, 0),
    ("user_id", "TEXT", 0, None, 0),
    ("email", "TEXT", 1, None, 0),
    ("token_hash", "TEXT", 1, None, 0),
    ("invite_id", "TEXT", 1, "''", 0),
    ("display_name", "TEXT", 1, "''", 0),
    ("language", "TEXT", 1, "'en'", 0),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
    ("expires_at", "TEXT", 1, None, 0),
    ("used_at", "TEXT", 1, "''", 0),
]
_CODE_COLUMNS = [
    ("id", "TEXT", 0, None, 1),
    ("user_id", "TEXT", 1, None, 0),
    ("code_hash", "TEXT", 1, None, 0),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
    ("used_at", "TEXT", 1, "''", 0),
]


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE sign_in_links (
            id TEXT PRIMARY KEY,
            purpose TEXT NOT NULL CHECK(purpose IN ('sign_in', 'signup')),
            user_id TEXT REFERENCES users(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            invite_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            language TEXT NOT NULL DEFAULT 'en',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute("CREATE INDEX idx_sign_in_links_email ON sign_in_links(email, created_at)")
    await db.execute(
        """CREATE TABLE mfa_recovery_codes (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            used_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute("CREATE INDEX idx_mfa_recovery_codes_user ON mfa_recovery_codes(user_id)")


async def _check(db: aiosqlite.Connection, table: str, expected: list) -> None:
    columns = await db.execute_fetchall(f"PRAGMA table_info({table})")
    actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual != expected:
        raise RuntimeError(f"Central {table} does not match migration 009")
    keys = await db.execute_fetchall(f"PRAGMA foreign_key_list({table})")
    if [(row["from"], row["table"], row["to"], row["on_delete"]) for row in keys] != [
        ("user_id", "users", "id", "CASCADE")
    ]:
        raise RuntimeError(f"Central {table}.user_id must cascade from users")


async def validate(db: aiosqlite.Connection) -> None:
    await _check(db, "sign_in_links", _LINK_COLUMNS)
    await _check(db, "mfa_recovery_codes", _CODE_COLUMNS)
