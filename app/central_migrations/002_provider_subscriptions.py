"""Add provider-neutral remote subscription lifecycle state."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE provider_subscription_registrations (
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            endpoint TEXT NOT NULL DEFAULT '',
            credential_key TEXT NOT NULL DEFAULT '',
            desired_revision INTEGER NOT NULL DEFAULT 1 CHECK(desired_revision >= 1),
            desired_state TEXT NOT NULL DEFAULT 'enabled'
                CHECK(desired_state IN ('enabled', 'disabled')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'active', 'degraded', 'error', 'disabling')),
            renewal_due_at TEXT NOT NULL DEFAULT '',
            last_reconciled_at TEXT NOT NULL DEFAULT '',
            next_reconcile_at TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            claim_token TEXT NOT NULL DEFAULT '',
            claim_owner TEXT NOT NULL DEFAULT '',
            claimed_until TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, provider),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT
        )"""
    )
    await db.execute(
        """CREATE TABLE provider_subscription_items (
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            subscription_key TEXT NOT NULL,
            remote_id TEXT NOT NULL,
            renew_at TEXT NOT NULL DEFAULT '',
            provider_data TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, provider, subscription_key),
            UNIQUE (provider, remote_id),
            FOREIGN KEY (user_id, provider)
                REFERENCES provider_subscription_registrations(user_id, provider)
                ON DELETE CASCADE
        )"""
    )
    await db.execute(
        """CREATE INDEX provider_subscription_registrations_due
           ON provider_subscription_registrations(next_reconcile_at, claimed_until)"""
    )
    await db.execute(
        """CREATE TABLE user_lifecycle_leases (
            user_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            claim_token TEXT NOT NULL,
            claimed_until TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )"""
    )

    await db.execute(
        """INSERT INTO provider_subscription_registrations
           (user_id, provider, desired_state, status, next_reconcile_at)
           SELECT routes.user_id, routes.provider,
                  CASE WHEN users.is_active = 1 THEN 'enabled' ELSE 'disabled' END,
                  CASE WHEN users.is_active = 1 THEN 'pending' ELSE 'disabling' END,
                  datetime('now')
           FROM webhook_routes AS routes
           JOIN users ON users.id = routes.user_id
           GROUP BY routes.user_id, routes.provider"""
    )


async def validate(db: aiosqlite.Connection) -> None:
    expected_columns = {
        "provider_subscription_registrations": [
            ("user_id", "TEXT", 1, None, 1),
            ("provider", "TEXT", 1, None, 2),
            ("endpoint", "TEXT", 1, "''", 0),
            ("credential_key", "TEXT", 1, "''", 0),
            ("desired_revision", "INTEGER", 1, "1", 0),
            ("desired_state", "TEXT", 1, "'enabled'", 0),
            ("status", "TEXT", 1, "'pending'", 0),
            ("renewal_due_at", "TEXT", 1, "''", 0),
            ("last_reconciled_at", "TEXT", 1, "''", 0),
            ("next_reconcile_at", "TEXT", 1, "''", 0),
            ("last_error", "TEXT", 1, "''", 0),
            ("claim_token", "TEXT", 1, "''", 0),
            ("claim_owner", "TEXT", 1, "''", 0),
            ("claimed_until", "TEXT", 1, "''", 0),
            ("created_at", "TEXT", 1, "datetime('now')", 0),
            ("updated_at", "TEXT", 1, "datetime('now')", 0),
        ],
        "provider_subscription_items": [
            ("user_id", "TEXT", 1, None, 1),
            ("provider", "TEXT", 1, None, 2),
            ("subscription_key", "TEXT", 1, None, 3),
            ("remote_id", "TEXT", 1, None, 0),
            ("renew_at", "TEXT", 1, "''", 0),
            ("provider_data", "TEXT", 1, "'{}'", 0),
            ("updated_at", "TEXT", 1, "datetime('now')", 0),
        ],
        "user_lifecycle_leases": [
            ("user_id", "TEXT", 0, None, 1),
            ("operation", "TEXT", 1, None, 0),
            ("claim_token", "TEXT", 1, None, 0),
            ("claimed_until", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, "datetime('now')", 0),
            ("updated_at", "TEXT", 1, "datetime('now')", 0),
        ],
    }
    for table, expected in expected_columns.items():
        rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
        actual = [(row["name"], row["type"], row["notnull"], row["dflt_value"], row["pk"]) for row in rows]
        if actual != expected:
            raise RuntimeError(f"Central table {table} does not match migration 002")

    registration_foreign_keys = await db.execute_fetchall(
        "PRAGMA foreign_key_list(provider_subscription_registrations)"
    )
    if [(row["table"], row["from"], row["to"], row["on_delete"]) for row in registration_foreign_keys] != [
        ("users", "user_id", "id", "RESTRICT")
    ]:
        raise RuntimeError("Provider subscription registration ownership constraint is missing")

    item_foreign_keys = await db.execute_fetchall("PRAGMA foreign_key_list(provider_subscription_items)")
    actual_item_keys = {(row["table"], row["from"], row["to"], row["on_delete"]) for row in item_foreign_keys}
    expected_item_keys = {
        ("provider_subscription_registrations", "user_id", "user_id", "CASCADE"),
        ("provider_subscription_registrations", "provider", "provider", "CASCADE"),
    }
    if actual_item_keys != expected_item_keys:
        raise RuntimeError("Provider subscription item ownership constraint is missing")

    lease_foreign_keys = await db.execute_fetchall("PRAGMA foreign_key_list(user_lifecycle_leases)")
    if [(row["table"], row["from"], row["to"], row["on_delete"]) for row in lease_foreign_keys] != [
        ("users", "user_id", "id", "CASCADE")
    ]:
        raise RuntimeError("User lifecycle lease ownership constraint is missing")

    rows = await db.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'provider_subscription_registrations'"
    )
    normalized_sql = "".join(rows[0]["sql"].lower().split()) if rows else ""
    if "check(desired_statein('enabled','disabled'))" not in normalized_sql:
        raise RuntimeError("Provider subscription desired-state constraint is missing")
    if "check(statusin('pending','active','degraded','error','disabling'))" not in normalized_sql:
        raise RuntimeError("Provider subscription status constraint is missing")
    if "check(desired_revision>=1)" not in normalized_sql:
        raise RuntimeError("Provider subscription desired revision constraint is missing")
    if await db.execute_fetchall("PRAGMA foreign_key_check"):
        raise RuntimeError("Central database has foreign key violations")
