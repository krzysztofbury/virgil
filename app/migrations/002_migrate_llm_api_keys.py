"""Migrate plaintext api_key column to encrypted api_key_enc."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(llm_providers)")
    col_names = [c[1] for c in cols]
    if "api_key" in col_names and "api_key_enc" not in col_names:
        from app.services.encryption import encrypt

        await db.execute("ALTER TABLE llm_providers RENAME COLUMN api_key TO api_key_enc")
        rows = await db.execute_fetchall("SELECT id, api_key_enc FROM llm_providers")
        for row in rows:
            val = row[1] if isinstance(row[1], str) else row["api_key_enc"]
            if not val.startswith("gAAAAA"):
                await db.execute(
                    "UPDATE llm_providers SET api_key_enc = ? WHERE id = ?",
                    (encrypt(val), row[0] if isinstance(row[0], int) else row["id"]),
                )
