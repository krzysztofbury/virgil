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


def _move_database_bundle(source: str, destination: str) -> list[tuple[str, str]]:
    """Move the DB and sidecars, restoring prior moves if any later move fails."""
    if os.path.exists(destination):
        raise FileExistsError(f"Destination database already exists: {destination}")
    moved: list[tuple[str, str]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            source_file = source + suffix
            if os.path.exists(source_file):
                destination_file = destination + suffix
                shutil.move(source_file, destination_file)
                moved.append((source_file, destination_file))
    except BaseException:
        for source_file, destination_file in reversed(moved):
            shutil.move(destination_file, source_file)
        raise
    return moved


def _restore_database_bundle(moved: list[tuple[str, str]]) -> None:
    for source_file, destination_file in reversed(moved):
        shutil.move(destination_file, source_file)


async def main():
    import aiosqlite

    from app.central_db import migrate_central_db
    from app.config import ADMIN_EMAILS, CENTRAL_DB_PATH, USERS_DB_DIR

    old_db_path = os.path.join(os.path.dirname(CENTRAL_DB_PATH), "virgil.db")
    if not os.path.exists(old_db_path):
        print(f"No existing database at {old_db_path} — nothing to migrate.")
        return

    # 1. Create central DB.
    os.makedirs(os.path.dirname(CENTRAL_DB_PATH), exist_ok=True)
    async with aiosqlite.connect(CENTRAL_DB_PATH) as central:
        central.row_factory = aiosqlite.Row
        await migrate_central_db(central)

        # 2. Read existing user.
        async with aiosqlite.connect(old_db_path) as old_db:
            old_db.row_factory = aiosqlite.Row
            try:
                rows = await old_db.execute_fetchall("SELECT * FROM auth_users WHERE id = 1")
            except Exception:
                print("No auth_users table in old database — already migrated?")
                return

        if not rows:
            print("No user found in auth_users — nothing to migrate.")
            return

        user = dict(rows[0])

        # 3. Create user in central DB.
        user_id = str(uuid.uuid4())
        db_filename = f"{user_id}.db"
        email = user["username"]
        role = "admin" if email.lower() in ADMIN_EMAILS else "user"

        # 4. Put the user database in place before publishing its registry row.
        os.makedirs(USERS_DB_DIR, exist_ok=True)
        new_path = os.path.join(USERS_DB_DIR, db_filename)
        moved = _move_database_bundle(old_db_path, new_path)
        try:
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
        except BaseException:
            await central.rollback()
            rows = await central.execute_fetchall("SELECT 1 FROM users WHERE id = ?", (user_id,))
            if not rows:
                _restore_database_bundle(moved)
            raise

    # 5. Drop auth_users from the moved DB.
    async with aiosqlite.connect(new_path) as moved_db:
        await moved_db.execute("DROP TABLE IF EXISTS auth_users")
        await moved_db.commit()

    print("Migration complete!")
    print(f"  User: {email} (role: {role})")
    print(f"  Central DB: {CENTRAL_DB_PATH}")
    print(f"  User DB: {new_path}")


if __name__ == "__main__":
    asyncio.run(main())
