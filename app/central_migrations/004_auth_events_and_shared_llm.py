"""A short log of security events, and whether an account may spend the server's LLM key."""

import aiosqlite

_EVENT_COLUMNS = [
    ("id", "INTEGER", 0, None, 1),
    ("created_at", "TEXT", 1, "datetime('now')", 0),
    ("user_id", "TEXT", 0, None, 0),
    ("event", "TEXT", 1, None, 0),
    ("ip", "TEXT", 1, "''", 0),
    ("detail", "TEXT", 1, "''", 0),
]


async def up(db: aiosqlite.Connection) -> None:
    # Every existing account keeps today's fallback to VIRGIL_INTERNAL_LLM_KEY;
    # create_user sets the column explicitly for new ones.
    await db.execute(
        "ALTER TABLE users ADD COLUMN allow_shared_llm INTEGER NOT NULL DEFAULT 1 CHECK(allow_shared_llm IN (0, 1))"
    )
    # Logins, sign-outs and account changes. No health data and no secrets; the
    # scheduler deletes rows older than 180 days, so a public form cannot fill the disk.
    await db.execute(
        """CREATE TABLE auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_id TEXT,
            event TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.execute("CREATE INDEX idx_auth_events_created_at ON auth_events(created_at)")


async def validate(db: aiosqlite.Connection) -> None:
    users = {row["name"]: row for row in await db.execute_fetchall("PRAGMA table_info(users)")}
    flag = users.get("allow_shared_llm")
    if flag is None or (flag["type"], flag["notnull"], flag["dflt_value"]) != ("INTEGER", 1, "1"):
        raise RuntimeError("Central users.allow_shared_llm does not match migration 004")
    columns = await db.execute_fetchall("PRAGMA table_info(auth_events)")
    actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in columns]
    if actual != _EVENT_COLUMNS:
        raise RuntimeError("Central auth_events does not match migration 004")
    indexes = {row["name"] for row in await db.execute_fetchall("PRAGMA index_list(auth_events)")}
    if "idx_auth_events_created_at" not in indexes:
        raise RuntimeError("Central auth_events is missing its created_at index")
