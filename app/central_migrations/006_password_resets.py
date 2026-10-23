"""Admin-issued password reset links: there is no mailer, so an admin hands the link over."""

import aiosqlite

_COLUMNS = [
    ("id", "TEXT", 0, None, 1),
    ("user_id", "TEXT", 1, None, 0),
    ("token_hash", "TEXT", 1, None, 0),
    ("created_by", "TEXT", 1, "''", 0),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
    ("expires_at", "TEXT", 1, None, 0),
    ("used_at", "TEXT", 1, "''", 0),
]


async def up(db: aiosqlite.Connection) -> None:
    # Only the SHA-256 of the link's secret is stored, and a link lives minutes.
    await db.execute(
        """CREATE TABLE password_resets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used_at TEXT NOT NULL DEFAULT ''
        )"""
    )


async def validate(db: aiosqlite.Connection) -> None:
    columns = await db.execute_fetchall("PRAGMA table_info(password_resets)")
    actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual != _COLUMNS:
        raise RuntimeError("Central password_resets does not match migration 006")
    keys = await db.execute_fetchall("PRAGMA foreign_key_list(password_resets)")
    if [(row["from"], row["table"], row["to"], row["on_delete"]) for row in keys] != [
        ("user_id", "users", "id", "CASCADE")
    ]:
        raise RuntimeError("Central password_resets.user_id must cascade from users")
