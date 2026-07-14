"""Central database — user registry for multi-user Virgil."""

import uuid
from pathlib import Path

import aiosqlite

from app.auth import hash_password
from app.config import ADMIN_EMAILS, CENTRAL_DB_PATH

_central_db: aiosqlite.Connection | None = None

CENTRAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
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
);

CREATE TABLE IF NOT EXISTS webhook_routes (
    webhook_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'oura',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


async def get_central_db() -> aiosqlite.Connection:
    """Return the central DB connection (singleton)."""
    global _central_db
    if _central_db is not None:
        try:
            await _central_db.execute("SELECT 1")
        except Exception:
            _central_db = None
    if _central_db is None:
        Path(CENTRAL_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _central_db = await aiosqlite.connect(CENTRAL_DB_PATH)
        _central_db.row_factory = aiosqlite.Row
        await _central_db.execute("PRAGMA journal_mode=WAL")
        await _central_db.execute("PRAGMA foreign_keys=ON")
        await _central_db.execute("PRAGMA busy_timeout=5000")
    return _central_db


async def init_central_db() -> None:
    """Create the users table if it doesn't exist."""
    db = await get_central_db()
    await db.executescript(CENTRAL_SCHEMA)
    await db.commit()


async def close_central_db() -> None:
    """Close the central DB connection."""
    global _central_db
    if _central_db:
        await _central_db.close()
        _central_db = None


async def create_user(email: str, password: str, display_name: str = "", only_if_first: bool = False) -> dict | None:
    """Create a new user. Returns the user dict, or None if only_if_first was
    set and another account already exists.

    only_if_first closes the bootstrap TOCTOU: with registration closed, two
    concurrent first signups both pass the count==0 check — the guarded INSERT
    lets exactly one of them win.
    """
    db = await get_central_db()
    user_id = str(uuid.uuid4())
    db_filename = f"{user_id}.db"
    pw_hash = hash_password(password)

    role = "admin" if email.lower() in ADMIN_EMAILS else "user"

    if only_if_first:
        cursor = await db.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role, db_filename)
               SELECT ?, ?, ?, ?, ?, ?
               WHERE (SELECT COUNT(*) FROM users) = 0""",
            (user_id, email.lower(), pw_hash, display_name, role, db_filename),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
    else:
        await db.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role, db_filename)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, email.lower(), pw_hash, display_name, role, db_filename),
        )
        await db.commit()
    return await get_user_by_id(user_id)


async def get_user_by_id(user_id: str) -> dict | None:
    """Look up user by ID."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT * FROM users WHERE id = ?", (user_id,))
    return dict(rows[0]) if rows else None


async def get_user_by_email(email: str) -> dict | None:
    """Look up user by email."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT * FROM users WHERE email = ?", (email.lower(),))
    return dict(rows[0]) if rows else None


async def get_all_users() -> list[dict]:
    """Return all users ordered by creation date."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT * FROM users ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def get_active_users() -> list[dict]:
    """Return all active users."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT * FROM users WHERE is_active = 1 ORDER BY created_at")
    return [dict(r) for r in rows]


_UPDATABLE_COLUMNS = frozenset(
    {
        "email",
        "password_hash",
        "display_name",
        "role",
        "is_active",
        "totp_secret",
        "totp_enabled",
        "last_login_at",
        # Factory reset repoints the account at a freshly created database.
        "db_filename",
    }
)


async def update_user(user_id: str, **fields) -> None:
    """Update specific fields on a user row."""
    if not fields:
        return
    for key in fields:
        if key not in _UPDATABLE_COLUMNS:
            raise ValueError(f"Invalid column for update: {key}")
    db = await get_central_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    await db.execute(f"UPDATE users SET {sets} WHERE id = ?", values)  # noqa: S608
    await db.commit()


async def delete_user(user_id: str) -> str | None:
    """Delete a user. Returns their db_filename for cleanup, or None."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT db_filename FROM users WHERE id = ?", (user_id,))
    if not rows:
        return None
    db_filename = rows[0]["db_filename"]
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return db_filename


async def count_users() -> int:
    """Total number of user accounts (active or not) — used for signup bootstrap."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) AS n FROM users")
    return rows[0]["n"]


async def get_primary_user_id() -> str | None:
    """Oldest account, active or not — keeps the legacy `virgil.md` export name
    permanently bound to the original install. Filtering on is_active made
    ownership FLIP whenever the first account was disabled: the next user
    silently inherited (and overwrote) the primary export file."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT id FROM users ORDER BY created_at LIMIT 1")
    return rows[0]["id"] if rows else None


# ── Webhook routing (public callbacks → per-user database) ──


async def create_webhook_route(user_id: str, provider: str = "oura") -> str:
    """Register an opaque webhook id for a user. Returns the webhook_id.

    Replaces any existing route for (user, provider) so re-enabling the webhook
    invalidates old callback URLs.
    """
    db = await get_central_db()
    webhook_id = uuid.uuid4().hex
    await db.execute("DELETE FROM webhook_routes WHERE user_id = ? AND provider = ?", (user_id, provider))
    await db.execute(
        "INSERT INTO webhook_routes (webhook_id, user_id, provider) VALUES (?, ?, ?)",
        (webhook_id, user_id, provider),
    )
    await db.commit()
    return webhook_id


async def get_webhook_route(webhook_id: str) -> dict | None:
    """Resolve a webhook_id to its active user, or None."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT u.* FROM webhook_routes wr
           JOIN users u ON u.id = wr.user_id
           WHERE wr.webhook_id = ? AND u.is_active = 1""",
        (webhook_id,),
    )
    return dict(rows[0]) if rows else None


async def get_all_webhook_ids() -> set[str]:
    """Every ACTIVE user's webhook ids — reconcile must not delete these
    when users share one Oura OAuth app."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT wr.webhook_id FROM webhook_routes wr
           JOIN users u ON u.id = wr.user_id
           WHERE u.is_active = 1"""
    )
    return {r["webhook_id"] for r in rows}


async def delete_webhook_routes(user_id: str, provider: str = "oura") -> None:
    """Remove webhook routes for a user/provider (webhook disabled)."""
    db = await get_central_db()
    await db.execute("DELETE FROM webhook_routes WHERE user_id = ? AND provider = ?", (user_id, provider))
    await db.commit()


async def promote_admin_emails() -> None:
    """Promote any existing users whose email matches VIRGIL_ADMIN_EMAILS."""
    if not ADMIN_EMAILS:
        return
    db = await get_central_db()
    for email in ADMIN_EMAILS:
        await db.execute(
            "UPDATE users SET role = 'admin' WHERE email = ? AND role != 'admin'",
            (email,),
        )
    await db.commit()
