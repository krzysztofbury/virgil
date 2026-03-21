"""One-time migration: convert single-user Virgil to multi-user.

Usage: cd virgil && uv run python scripts/migrate_to_multiuser.py

What it does:
1. Creates data/virgil-central.db with users table
2. Reads auth_users from data/virgil.db
3. Creates user row in central DB with new UUID
4. Moves data/virgil.db → data/users/{uuid}.db
5. Drops auth_users table from the moved DB
"""

import asyncio
import os
import shutil
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    import aiosqlite

    from app.central_db import CENTRAL_SCHEMA
    from app.config import ADMIN_EMAILS, CENTRAL_DB_PATH, USERS_DB_DIR

    old_db_path = os.path.join(os.path.dirname(CENTRAL_DB_PATH), "virgil.db")
    if not os.path.exists(old_db_path):
        print(f"No existing database at {old_db_path} — nothing to migrate.")
        return

    # 1. Create central DB.
    os.makedirs(os.path.dirname(CENTRAL_DB_PATH), exist_ok=True)
    central = await aiosqlite.connect(CENTRAL_DB_PATH)
    central.row_factory = aiosqlite.Row
    await central.executescript(CENTRAL_SCHEMA)
    await central.commit()

    # 2. Read existing user.
    old_db = await aiosqlite.connect(old_db_path)
    old_db.row_factory = aiosqlite.Row
    try:
        rows = await old_db.execute_fetchall("SELECT * FROM auth_users WHERE id = 1")
    except Exception:
        print("No auth_users table in old database — already migrated?")
        await old_db.close()
        await central.close()
        return

    if not rows:
        print("No user found in auth_users — nothing to migrate.")
        await old_db.close()
        await central.close()
        return

    user = dict(rows[0])
    await old_db.close()

    # 3. Create user in central DB.
    user_id = str(uuid.uuid4())
    db_filename = f"{user_id}.db"
    email = user["username"]
    role = "admin" if email.lower() in ADMIN_EMAILS else "user"

    await central.execute(
        """INSERT INTO users (id, email, password_hash, display_name, role, db_filename,
           totp_secret, totp_enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            email,
            user["password_hash"],
            email,
            role,
            db_filename,
            user.get("totp_secret", ""),
            1 if user.get("totp_enabled") else 0,
        ),
    )
    await central.commit()
    await central.close()

    # 4. Move old DB to per-user location.
    os.makedirs(USERS_DB_DIR, exist_ok=True)
    new_path = os.path.join(USERS_DB_DIR, db_filename)
    shutil.move(old_db_path, new_path)

    # Move WAL/SHM if present.
    for suffix in ("-wal", "-shm"):
        old_wal = old_db_path + suffix
        if os.path.exists(old_wal):
            shutil.move(old_wal, new_path + suffix)

    # 5. Drop auth_users from the moved DB.
    moved_db = await aiosqlite.connect(new_path)
    await moved_db.execute("DROP TABLE IF EXISTS auth_users")
    await moved_db.commit()
    await moved_db.close()

    print("Migration complete!")
    print(f"  User: {email} (role: {role})")
    print(f"  Central DB: {CENTRAL_DB_PATH}")
    print(f"  User DB: {new_path}")


if __name__ == "__main__":
    asyncio.run(main())
