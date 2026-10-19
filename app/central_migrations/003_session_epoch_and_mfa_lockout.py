"""Revocable sessions and a per-account limit on failed MFA codes."""

import aiosqlite

_COLUMNS = [
    # Signed into every session cookie; bumping it refuses every older session.
    ("session_epoch", "INTEGER NOT NULL DEFAULT 0"),
    ("mfa_failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
    # ISO-8601 UTC; empty when verification is not locked.
    ("mfa_locked_until", "TEXT NOT NULL DEFAULT ''"),
]


async def up(db: aiosqlite.Connection) -> None:
    for name, definition in _COLUMNS:
        await db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")


async def validate(db: aiosqlite.Connection) -> None:
    rows = await db.execute_fetchall("PRAGMA table_info(users)")
    actual = {row["name"]: (row["type"], row["notnull"], row["dflt_value"]) for row in rows}
    expected = {
        "session_epoch": ("INTEGER", 1, "0"),
        "mfa_failed_attempts": ("INTEGER", 1, "0"),
        "mfa_locked_until": ("TEXT", 1, "''"),
    }
    for name, shape in expected.items():
        if actual.get(name) != shape:
            raise RuntimeError(f"Central users.{name} does not match migration 003")
