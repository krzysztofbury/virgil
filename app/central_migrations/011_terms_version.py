"""Which version of the terms and the privacy policy an account accepted (#182).

users.terms_version is the version accepted at signup. A signup link carries
the version its form showed. The consent box shipped with migration 010, and a
signup link lives 15 minutes, so every signup link waiting at upgrade time was
sent from a form with the box ticked: those get the first version, 2026-10-28.
Accounts made before stay NULL.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE users ADD COLUMN terms_version TEXT")
    await db.execute("ALTER TABLE sign_in_links ADD COLUMN terms_version TEXT")
    await db.execute("UPDATE sign_in_links SET terms_version = '2026-10-28' WHERE purpose = 'signup'")


async def validate(db: aiosqlite.Connection) -> None:
    for table in ("users", "sign_in_links"):
        columns = {row["name"] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
        if "terms_version" not in columns:
            raise RuntimeError(f"Central {table}.terms_version does not match migration 011")
