"""Fix a per-user DB migrated from old single-user backup.

Usage: uv run python scripts/fix_user_db.py data/users/9dea4adb-93ba-41ec-a654-e27d20115110.db
"""

import asyncio
import importlib
import os
import sys

# Add project root to path so `app.migrations` can be imported.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite


async def main(db_path: str):
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row

    rows = await db.execute_fetchall("SELECT MAX(version) as v FROM schema_migrations")
    current = rows[0]["v"] if rows and rows[0]["v"] else 0
    print(f"Current version: {current}")

    await db.execute("DROP TABLE IF EXISTS auth_users")
    print("Dropped auth_users")

    # Apply all missing migrations in order.
    if current < 6:
        mod = importlib.import_module("app.migrations.006_training_overhaul")
        await mod.up(db)
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (6, '006_training_overhaul.py')"
        )
        await db.commit()
        print("Applied 006")

    if current < 7:
        mod = importlib.import_module("app.migrations.007_litellm_model_strings")
        await mod.up(db)
        await db.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (7, '007_litellm_model_strings.py')"
        )
        await db.commit()
        print("Applied 007")

    if current < 8:
        mod = importlib.import_module("app.migrations.008_onboarding")
        await mod.up(db)
        await db.execute("INSERT OR IGNORE INTO schema_migrations (version, name) VALUES (8, '008_onboarding.py')")
        await db.commit()
        print("Applied 008")

    await db.execute(
        "INSERT INTO app_settings (key, value) VALUES ('onboarding_completed', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = '1'"
    )
    await db.commit()
    print("Set onboarding_completed=1")

    rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM daily_logs")
    print(f"Daily logs: {rows[0]['c']}")
    rows = await db.execute_fetchall("SELECT COUNT(*) as c FROM training_sessions")
    print(f"Training sessions: {rows[0]['c']}")

    await db.close()
    print("Done!")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: uv run python scripts/fix_user_db.py <path-to-user-db>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
