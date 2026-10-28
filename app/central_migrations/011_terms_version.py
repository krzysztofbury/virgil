"""Which version of the terms and the privacy policy an account accepted (#182).

users.terms_version is the version accepted at signup. A signup link carries
the version its form showed, so an account made from a link sent before the
consent box existed records no acceptance. Both stay NULL for older rows.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("ALTER TABLE users ADD COLUMN terms_version TEXT")
    await db.execute("ALTER TABLE sign_in_links ADD COLUMN terms_version TEXT")


async def validate(db: aiosqlite.Connection) -> None:
    for table in ("users", "sign_in_links"):
        columns = {row["name"] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
        if "terms_version" not in columns:
            raise RuntimeError(f"Central {table}.terms_version does not match migration 011")
