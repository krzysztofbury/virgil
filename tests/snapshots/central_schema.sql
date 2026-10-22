-- index idx_auth_events_created_at
CREATE INDEX idx_auth_events_created_at ON auth_events(created_at);

-- index provider_subscription_registrations_due
CREATE INDEX provider_subscription_registrations_due
           ON provider_subscription_registrations(next_reconcile_at, claimed_until);

-- table auth_events
CREATE TABLE auth_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            user_id TEXT,
            event TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        );

-- table central_schema_migrations
CREATE TABLE central_schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

-- table provider_subscription_items
CREATE TABLE provider_subscription_items (
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
        );

-- table provider_subscription_registrations
CREATE TABLE provider_subscription_registrations (
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
        );

-- table user_lifecycle_leases
CREATE TABLE user_lifecycle_leases (
            user_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            claim_token TEXT NOT NULL,
            claimed_until TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

-- table users
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT DEFAULT 'user' CHECK(role IN ('user', 'admin')),
    db_filename TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    totp_secret TEXT,
    totp_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_login_at TEXT
, session_epoch INTEGER NOT NULL DEFAULT 0, mfa_failed_attempts INTEGER NOT NULL DEFAULT 0, mfa_locked_until TEXT NOT NULL DEFAULT '', allow_shared_llm INTEGER NOT NULL DEFAULT 1 CHECK(allow_shared_llm IN (0, 1)));

-- table webhook_routes
CREATE TABLE webhook_routes (
    webhook_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'oura',
    created_at TEXT DEFAULT (datetime('now'))
);
