"""Each account's interface language (#152).

It lives in the registry because the language is needed before any account's
database is open: the sign-in pages and, later, emails sent to the account.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en' CHECK(language IN ('en', 'pl'))")


async def validate(db: aiosqlite.Connection) -> None:
    users = {row["name"]: row for row in await db.execute_fetchall("PRAGMA table_info(users)")}
    column = users.get("language")
    if column is None or (column["type"], column["notnull"], column["dflt_value"]) != ("TEXT", 1, "'en'"):
        raise RuntimeError("Central users.language does not match migration 008")
