"""Baseline the existing users and webhook routing schema."""

import aiosqlite

from app.central_schema import CENTRAL_SCHEMA_V1


async def up(db: aiosqlite.Connection) -> None:
    # execute(), not executescript(): executescript commits implicitly and would
    # escape the runner's transaction on a partially applied baseline.
    for statement in CENTRAL_SCHEMA_V1.split(";"):
        if sql := statement.strip():
            await db.execute(sql)


async def validate(db: aiosqlite.Connection) -> None:
    expected_tables = {
        "users": [
            ("id", "TEXT", 0, None, 1),
            ("email", "TEXT", 1, None, 0),
            ("password_hash", "TEXT", 1, None, 0),
            ("display_name", "TEXT", 0, None, 0),
            ("role", "TEXT", 0, "'user'", 0),
            ("db_filename", "TEXT", 1, None, 0),
            ("is_active", "INTEGER", 0, "1", 0),
            ("totp_secret", "TEXT", 0, None, 0),
            ("totp_enabled", "INTEGER", 0, "0", 0),
            ("created_at", "TEXT", 0, "datetime('now')", 0),
            ("last_login_at", "TEXT", 0, None, 0),
        ],
        "webhook_routes": [
            ("webhook_id", "TEXT", 0, None, 1),
            ("user_id", "TEXT", 1, None, 0),
            ("provider", "TEXT", 1, "'oura'", 0),
            ("created_at", "TEXT", 0, "datetime('now')", 0),
        ],
    }
    for table, expected_columns in expected_tables.items():
        rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
        columns = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in rows]
        if columns != expected_columns:
            raise RuntimeError(f"Central table {table} does not match migration 001")

    indexes = await db.execute_fetchall("PRAGMA index_list(users)")
    unique_email = False
    for index in indexes:
        if index["unique"] != 1 or index["partial"] != 0:
            continue
        columns = await db.execute_fetchall("SELECT name FROM pragma_index_info(?)", (index["name"],))
        if [column["name"] for column in columns] == ["email"]:
            unique_email = True
            break
    if not unique_email:
        raise RuntimeError("Central users.email UNIQUE constraint is missing")

    foreign_keys = await db.execute_fetchall("PRAGMA foreign_key_list(webhook_routes)")
    expected_foreign_key = ("users", "user_id", "id", "CASCADE")
    actual_foreign_keys = [(row["table"], row["from"], row["to"], row["on_delete"]) for row in foreign_keys]
    if actual_foreign_keys != [expected_foreign_key]:
        raise RuntimeError("Central webhook_routes user foreign key does not match migration 001")
    if await db.execute_fetchall("PRAGMA foreign_key_check"):
        raise RuntimeError("Central database has foreign key violations")

    rows = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'")
    normalized_sql = "".join(rows[0]["sql"].lower().split()) if rows else ""
    if "check(rolein('user','admin'))" not in normalized_sql:
        raise RuntimeError("Central users.role CHECK constraint is missing")
