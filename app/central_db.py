"""Central database — user registry for multi-user Virgil."""

import asyncio
import fcntl
import hashlib
import logging
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from app.auth import hash_password_async
from app.config import ADMIN_EMAILS, CENTRAL_DB_PATH

logger = logging.getLogger(__name__)

_central_db: aiosqlite.Connection | None = None
CENTRAL_MIGRATION_LOCK_TIMEOUT_SECONDS = 30.0


@asynccontextmanager
async def _central_migration_lock(db: aiosqlite.Connection) -> AsyncIterator[None]:
    from app.services.backup import db_main_path

    database_path = Path(await db_main_path(db))
    lock_path = database_path.with_name(f".{database_path.name}.migration.lock")
    handle = lock_path.open("a+b")
    deadline = asyncio.get_running_loop().time() + CENTRAL_MIGRATION_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(f"Timed out waiting for central migration lock: {lock_path.name}") from None
                await asyncio.sleep(0.05)
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


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


@asynccontextmanager
async def _subscription_connection() -> AsyncIterator[aiosqlite.Connection]:
    """Use a dedicated connection so lifecycle transactions cannot interleave."""
    control = await get_central_db()
    databases = await control.execute_fetchall("PRAGMA database_list")
    database_path = next((row["file"] for row in databases if row["name"] == "main"), "")
    if not database_path:
        raise RuntimeError("Provider subscription lifecycle requires a file-backed central database")
    db = await aiosqlite.connect(database_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        await db.close()


async def migrate_central_db(db: aiosqlite.Connection) -> None:
    """Serialize snapshot and migration so every caller uses the same safety boundary."""
    from app.central_migrations.runner import (
        _current_version,
        count_pending_migrations,
        has_application_schema,
        run_migrations,
    )
    from app.services.backup import snapshot_central_before_migration

    async with _central_migration_lock(db):
        if await count_pending_migrations(db) > 0 and await has_application_schema(db):
            await snapshot_central_before_migration(db, await _current_version(db))
        await run_migrations(db)


async def init_central_db() -> None:
    """Open and migrate the central registry to the current version."""
    await migrate_central_db(await get_central_db())


async def close_central_db() -> None:
    """Close the central DB connection."""
    global _central_db
    if _central_db:
        await _central_db.close()
        _central_db = None


async def create_user(
    email: str,
    password: str,
    display_name: str = "",
    only_if_first: bool = False,
    *,
    allow_shared_llm: bool = False,
) -> dict | None:
    """Create a new user. Returns the user dict, or None if only_if_first was
    set and another account already exists.

    only_if_first closes the bootstrap TOCTOU: with registration closed, two
    concurrent first signups both pass the count==0 check — the guarded INSERT
    lets exactly one of them win.

    The first account is the operator's and may always spend the server's LLM
    key; any later one only when `allow_shared_llm` says so. The column is set
    on every insert: its SQL default of 1 exists for accounts that predate it.
    """
    db = await get_central_db()
    user_id = str(uuid.uuid4())
    db_filename = f"{user_id}.db"
    pw_hash = await hash_password_async(password)

    role = "admin" if email.lower() in ADMIN_EMAILS else "user"

    if only_if_first:
        cursor = await db.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role, db_filename, allow_shared_llm)
               SELECT ?, ?, ?, ?, ?, ?, 1
               WHERE (SELECT COUNT(*) FROM users) = 0""",
            (user_id, email.lower(), pw_hash, display_name, role, db_filename),
        )
        await db.commit()
        if cursor.rowcount == 0:
            return None
    else:
        await db.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role, db_filename, allow_shared_llm)
               SELECT ?, ?, ?, ?, ?, ?, CASE WHEN (SELECT COUNT(*) FROM users) = 0 THEN 1 ELSE ? END""",
            (user_id, email.lower(), pw_hash, display_name, role, db_filename, int(allow_shared_llm)),
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
        "allow_shared_llm",
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


async def bump_session_epoch(user_id: str) -> int:
    """Revoke every session of this account; return the new epoch to sign fresh ones with."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "UPDATE users SET session_epoch = session_epoch + 1 WHERE id = ? RETURNING session_epoch", (user_id,)
    )
    await db.commit()
    if not rows:
        raise LookupError(f"No account {user_id}")
    return rows[0]["session_epoch"]


def _utc_stamp(moment: datetime) -> str:
    """Fixed-width UTC, so the stamps compare correctly as strings in SQL."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def claim_mfa_attempt(user_id: str, now: datetime, max_attempts: int, lock_seconds: int) -> bool:
    """Count one MFA code attempt before checking it. False while verification is locked.

    Counting first keeps parallel guesses inside the limit: each one takes a number,
    and any beyond `max_attempts` is refused and locks verification.
    """
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """UPDATE users SET mfa_failed_attempts = mfa_failed_attempts + 1
           WHERE id = ? AND mfa_locked_until <= ?
           RETURNING mfa_failed_attempts""",
        (user_id, _utc_stamp(now)),
    )
    await db.commit()
    if not rows:
        return False
    if rows[0]["mfa_failed_attempts"] > max_attempts:
        await _lock_mfa(db, user_id, now, lock_seconds)
        return False
    return True


async def _lock_mfa(db: aiosqlite.Connection, user_id: str, now: datetime, lock_seconds: int) -> None:
    await db.execute(
        "UPDATE users SET mfa_failed_attempts = 0, mfa_locked_until = ? WHERE id = ?",
        (_utc_stamp(now + timedelta(seconds=lock_seconds)), user_id),
    )
    await db.commit()


async def finish_mfa_attempt(user_id: str, ok: bool, now: datetime, max_attempts: int, lock_seconds: int) -> None:
    """Clear the count after a right code; lock verification once a wrong one reaches the limit."""
    db = await get_central_db()
    if ok:
        await db.execute("UPDATE users SET mfa_failed_attempts = 0, mfa_locked_until = '' WHERE id = ?", (user_id,))
        await db.commit()
        return
    rows = await db.execute_fetchall("SELECT mfa_failed_attempts FROM users WHERE id = ?", (user_id,))
    if rows and rows[0]["mfa_failed_attempts"] >= max_attempts:
        await _lock_mfa(db, user_id, now, lock_seconds)


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


async def disable_user_if_unsubscribed(user_id: str) -> str:
    """Atomically disable a user unless remote lifecycle state still exists."""
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            users = await db.execute_fetchall("SELECT 1 FROM users WHERE id = ?", (user_id,))
            if not users:
                await db.rollback()
                return "not_found"
            leases = await db.execute_fetchall(
                """SELECT 1 FROM user_lifecycle_leases
                   WHERE user_id = ? AND claimed_until > ? LIMIT 1""",
                (user_id, datetime.now(UTC).isoformat()),
            )
            if leases:
                await db.rollback()
                return "busy"
            subscriptions = await db.execute_fetchall(
                "SELECT 1 FROM provider_subscription_registrations WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            if subscriptions:
                await db.rollback()
                return "subscribed"
            # Re-enabling the account later must not revive the sessions it had.
            await db.execute(
                "UPDATE users SET is_active = 0, session_epoch = session_epoch + 1 WHERE id = ?", (user_id,)
            )
            await db.commit()
            return "disabled"
        except BaseException:
            await db.rollback()
            raise


async def delete_user_if_unsubscribed(user_id: str) -> tuple[str, str | None]:
    """Atomically delete a user unless remote lifecycle state still exists."""
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            users = await db.execute_fetchall("SELECT db_filename FROM users WHERE id = ?", (user_id,))
            if not users:
                await db.rollback()
                return "not_found", None
            leases = await db.execute_fetchall(
                """SELECT 1 FROM user_lifecycle_leases
                   WHERE user_id = ? AND claimed_until > ? LIMIT 1""",
                (user_id, datetime.now(UTC).isoformat()),
            )
            if leases:
                await db.rollback()
                return "busy", None
            subscriptions = await db.execute_fetchall(
                "SELECT 1 FROM provider_subscription_registrations WHERE user_id = ? LIMIT 1",
                (user_id,),
            )
            if subscriptions:
                await db.rollback()
                return "subscribed", None
            db_filename = users[0]["db_filename"]
            await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            await db.commit()
            return "deleted", db_filename
        except BaseException:
            await db.rollback()
            raise


AUTH_EVENTS = frozenset(
    {
        "signup",
        "login_ok",
        "login_fail",
        "mfa_fail",
        "mfa_lock",
        "mfa_enable",
        "mfa_disable",
        "logout",
        "password_change",
        "factory_reset",
        "invite_create",
        "invite_revoke",
        "invite_consume",
        "role_change",
        "reset_link_create",
        "password_reset",
        "llm_access_change",
        "user_disable",
        "user_enable",
        "user_delete",
    }
)
AUTH_EVENT_RETENTION_DAYS = 180


async def record_auth_event(event: str, *, user_id: str | None = None, ip: str = "", detail: str = "") -> None:
    """Append one security event. A failed write is logged and never blocks the sign-in it describes."""
    if event not in AUTH_EVENTS:
        raise ValueError(f"Unknown auth event: {event}")
    try:
        db = await get_central_db()
        await db.execute(
            "INSERT INTO auth_events (user_id, event, ip, detail) VALUES (?, ?, ?, ?)",
            (user_id, event, ip[:64], detail[:200]),
        )
        await db.commit()
    except Exception:
        logger.exception("Could not record auth event %s", event)


async def list_auth_events(limit: int = 50) -> list[dict]:
    """The newest events first, with the account's current email when it still exists."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT e.created_at, e.event, e.ip, e.detail, e.user_id, u.email
           FROM auth_events e LEFT JOIN users u ON u.id = e.user_id
           ORDER BY e.id DESC LIMIT ?""",
        (limit,),
    )
    return [dict(row) for row in rows]


async def prune_auth_events(days: int = AUTH_EVENT_RETENTION_DAYS) -> int:
    """Delete events older than `days`; return how many went."""
    db = await get_central_db()
    cursor = await db.execute("DELETE FROM auth_events WHERE created_at < datetime('now', ?)", (f"-{int(days)} days",))
    await db.commit()
    return cursor.rowcount


INVITE_TTL_DAYS = 7


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create_invite(
    created_by: str, *, email: str = "", role: str = "user", allow_shared_llm: bool = False, days: int = INVITE_TTL_DAYS
) -> str:
    """Store a single-use invite and return the link's secret. Only its hash is kept."""
    if role not in ("user", "admin"):
        raise ValueError(f"Unknown role: {role}")
    token = secrets.token_urlsafe(32)
    db = await get_central_db()
    await db.execute(
        """INSERT INTO invites (id, token_hash, email, role, allow_shared_llm, created_by, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            _token_hash(token),
            email.strip().lower(),
            role,
            int(allow_shared_llm),
            created_by,
            _utc_stamp(datetime.now(UTC) + timedelta(days=days)),
        ),
    )
    await db.commit()
    return token


# An invite counts only while it is unused, unexpired, and its issuer is still an
# active admin: demoting or disabling an admin withdraws what they vouched for.
_OPEN_INVITE = """SELECT i.* FROM invites i JOIN users u ON u.id = i.created_by
                  WHERE i.token_hash = ? AND i.used_at = '' AND i.expires_at > ?
                    AND u.role = 'admin' AND u.is_active = 1"""


async def get_open_invite(token: str) -> dict | None:
    """The usable invite behind a link, or None."""
    if not token:
        return None
    db = await get_central_db()
    rows = await db.execute_fetchall(_OPEN_INVITE, (_token_hash(token), _utc_stamp(datetime.now(UTC))))
    return dict(rows[0]) if rows else None


async def list_open_invites() -> list[dict]:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT i.id, i.email, i.role, i.allow_shared_llm, i.created_at, i.expires_at, u.email AS created_by_email
           FROM invites i JOIN users u ON u.id = i.created_by
           WHERE i.used_at = '' AND i.expires_at > ? AND u.role = 'admin' AND u.is_active = 1
           ORDER BY i.created_at DESC""",
        (_utc_stamp(datetime.now(UTC)),),
    )
    return [dict(row) for row in rows]


async def revoke_invites_created_by(user_id: str) -> None:
    """Drop the unused invites of an admin who was demoted or disabled."""
    db = await get_central_db()
    await db.execute("DELETE FROM invites WHERE created_by = ? AND used_at = ''", (user_id,))
    await db.commit()


async def revoke_invite(invite_id: str) -> bool:
    db = await get_central_db()
    cursor = await db.execute("DELETE FROM invites WHERE id = ? AND used_at = ''", (invite_id,))
    await db.commit()
    return cursor.rowcount == 1


async def create_user_from_invite(
    token: str, email: str, password: str, display_name: str = ""
) -> tuple[str, dict | None]:
    """Create the account an invite allows and use the invite up, in one transaction.

    Returns ("created", user), or ("invalid" | "email_mismatch" | "email_reserved" |
    "email_taken", None) with the invite left untouched, so a typo does not burn the link.

    The account gets the invite's role. An address in VIRGIL_ADMIN_EMAILS is an
    admin after every restart and nothing verifies email ownership, so such an
    address only comes in through an invite made for exactly that address.
    """
    email = email.strip().lower()
    pw_hash = await hash_password_async(password)
    user_id = str(uuid.uuid4())
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = await db.execute_fetchall(_OPEN_INVITE, (_token_hash(token), _utc_stamp(datetime.now(UTC))))
            status = "created"
            if not rows:
                status = "invalid"
            elif rows[0]["email"] and rows[0]["email"] != email:
                status = "email_mismatch"
            elif email in ADMIN_EMAILS and rows[0]["email"] != email:
                status = "email_reserved"
            elif await db.execute_fetchall("SELECT 1 FROM users WHERE email = ?", (email,)):
                status = "email_taken"
            if status != "created":
                await db.rollback()
                return status, None
            invite = rows[0]
            role = "admin" if invite["role"] == "admin" or email in ADMIN_EMAILS else "user"
            await db.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role, db_filename, allow_shared_llm)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, email, pw_hash, display_name, role, f"{user_id}.db", invite["allow_shared_llm"]),
            )
            await db.execute(
                "UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?",
                (_utc_stamp(datetime.now(UTC)), user_id, invite["id"]),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return "created", await get_user_by_id(user_id)


async def reopen_invite_used_by(user_id: str) -> None:
    """Give an invite back when the account it created could not be provisioned."""
    db = await get_central_db()
    await db.execute("UPDATE invites SET used_at = '', used_by = '' WHERE used_by = ?", (user_id,))
    await db.commit()


RESET_TTL_MINUTES = 15

_OPEN_RESET = """SELECT r.*, u.email FROM password_resets r JOIN users u ON u.id = r.user_id
                 WHERE r.token_hash = ? AND r.used_at = '' AND r.expires_at > ? AND u.is_active = 1"""


async def create_password_reset(user_id: str, created_by: str, minutes: int = RESET_TTL_MINUTES) -> str:
    """Store a single-use reset link for an account and return its secret.

    Any earlier unused link for the account stops working: only the newest one
    an admin handed over should open the form."""
    token = secrets.token_urlsafe(32)
    db = await get_central_db()
    await db.execute("DELETE FROM password_resets WHERE user_id = ? AND used_at = ''", (user_id,))
    await db.execute(
        "INSERT INTO password_resets (id, user_id, token_hash, created_by, expires_at) VALUES (?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            user_id,
            _token_hash(token),
            created_by,
            _utc_stamp(datetime.now(UTC) + timedelta(minutes=minutes)),
        ),
    )
    await db.commit()
    return token


async def get_open_reset(token: str) -> dict | None:
    """The usable reset behind a link, with the account's email, or None."""
    if not token:
        return None
    db = await get_central_db()
    rows = await db.execute_fetchall(_OPEN_RESET, (_token_hash(token), _utc_stamp(datetime.now(UTC))))
    return dict(rows[0]) if rows else None


async def complete_password_reset(token: str, password: str) -> str | None:
    """Set the new password, sign out every session and use the link up, in one
    transaction. Returns the account id, or None when the link is not usable."""
    pw_hash = await hash_password_async(password)
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            rows = await db.execute_fetchall(_OPEN_RESET, (_token_hash(token), _utc_stamp(datetime.now(UTC))))
            if not rows:
                await db.rollback()
                return None
            user_id = rows[0]["user_id"]
            # MFA stays as it was: a reset link replaces the password, not the second factor.
            await db.execute(
                "UPDATE users SET password_hash = ?, session_epoch = session_epoch + 1 WHERE id = ?",
                (pw_hash, user_id),
            )
            await db.execute(
                "UPDATE password_resets SET used_at = ? WHERE id = ?", (_utc_stamp(datetime.now(UTC)), rows[0]["id"])
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return user_id


async def shared_llm_allowed(db_filename: str) -> bool:
    """Whether the active account that owns this database may spend the server's LLM key.

    A database no account owns may not: the key is the operator's money."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT allow_shared_llm FROM users WHERE db_filename = ? AND is_active = 1", (db_filename,)
    )
    return bool(rows and rows[0]["allow_shared_llm"])


async def count_users() -> int:
    """Total number of user accounts (active or not) — used for signup bootstrap."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT COUNT(*) AS n FROM users")
    return rows[0]["n"]


# ── Webhook routing (public callbacks → per-user database) ──


async def create_webhook_route(
    user_id: str,
    provider: str = "oura",
    *,
    lifecycle_claim: str = "",
) -> str:
    """Return a stable opaque callback route for a user/provider."""
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            users = await db.execute_fetchall("SELECT is_active FROM users WHERE id = ?", (user_id,))
            if not users or users[0]["is_active"] != 1:
                raise RuntimeError("Active user is required for a webhook route")
            await _require_no_user_lifecycle_lease(db, user_id, lifecycle_claim)
            rows = await db.execute_fetchall(
                """SELECT webhook_id FROM webhook_routes
                   WHERE user_id = ? AND provider = ? ORDER BY created_at DESC, rowid DESC""",
                (user_id, provider),
            )
            if rows:
                webhook_id = rows[0]["webhook_id"]
            else:
                webhook_id = uuid.uuid4().hex
                await db.execute(
                    "INSERT INTO webhook_routes (webhook_id, user_id, provider) VALUES (?, ?, ?)",
                    (webhook_id, user_id, provider),
                )
            await db.commit()
            return webhook_id
        except BaseException:
            await db.rollback()
            raise


async def get_webhook_route(webhook_id: str, provider: str = "oura") -> dict | None:
    """Resolve a webhook_id to its active user, or None."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT u.* FROM webhook_routes wr
           JOIN users u ON u.id = wr.user_id
           WHERE wr.webhook_id = ? AND wr.provider = ? AND u.is_active = 1""",
        (webhook_id, provider),
    )
    return dict(rows[0]) if rows else None


async def get_all_webhook_ids(provider: str = "oura") -> set[str]:
    """Every existing user's webhook ids — reconcile must not delete these
    when users share one Oura OAuth app. Deliberately NOT filtered on
    is_active: a temporarily disabled account keeps its Oura subscription and
    resumes syncing on re-enable; hard-deleted users vanish via the JOIN
    (webhook_routes rows cascade on user deletion)."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT wr.webhook_id FROM webhook_routes wr
           JOIN users u ON u.id = wr.user_id
           WHERE wr.provider = ?""",
        (provider,),
    )
    return {r["webhook_id"] for r in rows}


async def get_webhook_id_for_user(user_id: str, provider: str) -> str | None:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT webhook_id FROM webhook_routes WHERE user_id = ? AND provider = ?
           ORDER BY created_at DESC, rowid DESC""",
        (user_id, provider),
    )
    return rows[0]["webhook_id"] if rows else None


async def get_webhook_ids_for_user(user_id: str, provider: str) -> set[str]:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT webhook_id FROM webhook_routes WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    )
    return {row["webhook_id"] for row in rows}


async def delete_webhook_routes(user_id: str, provider: str = "oura") -> None:
    """Remove webhook routes for a user/provider (webhook disabled)."""
    db = await get_central_db()
    await db.execute("DELETE FROM webhook_routes WHERE user_id = ? AND provider = ?", (user_id, provider))
    await db.commit()


# ── Provider subscription lifecycle ──


async def _require_no_user_lifecycle_lease(
    db: aiosqlite.Connection,
    user_id: str,
    allowed_claim_token: str = "",
) -> None:
    now = datetime.now(UTC).isoformat()
    rows = await db.execute_fetchall(
        """SELECT operation FROM user_lifecycle_leases
           WHERE user_id = ? AND claimed_until > ? AND claim_token != ?""",
        (user_id, now, allowed_claim_token),
    )
    if rows:
        raise RuntimeError(f"User lifecycle operation is in progress: {rows[0]['operation']}")


async def claim_user_lifecycle(
    user_id: str,
    operation: str,
    *,
    lease_seconds: int = 600,
) -> str | None:
    if not operation or len(operation) > 100:
        raise ValueError("User lifecycle operation is required and bounded")
    if not 30 <= lease_seconds <= 3600:
        raise ValueError("User lifecycle lease must be between 30 and 3600 seconds")
    now = datetime.now(UTC)
    claim_token = uuid.uuid4().hex
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            users = await db.execute_fetchall("SELECT 1 FROM users WHERE id = ?", (user_id,))
            if not users:
                await db.rollback()
                return None
            cursor = await db.execute(
                """INSERT INTO user_lifecycle_leases
                   (user_id, operation, claim_token, claimed_until, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       operation = excluded.operation,
                       claim_token = excluded.claim_token,
                       claimed_until = excluded.claimed_until,
                       updated_at = excluded.updated_at
                   WHERE user_lifecycle_leases.claimed_until <= ?""",
                (
                    user_id,
                    operation,
                    claim_token,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            await db.commit()
            return claim_token if cursor.rowcount == 1 else None
        except BaseException:
            await db.rollback()
            raise


async def release_user_lifecycle(user_id: str, claim_token: str) -> None:
    async with _subscription_connection() as db:
        cursor = await db.execute(
            "DELETE FROM user_lifecycle_leases WHERE user_id = ? AND claim_token = ?",
            (user_id, claim_token),
        )
        await db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("User lifecycle lease was lost")


async def heartbeat_user_lifecycle(
    user_id: str,
    claim_token: str,
    *,
    lease_seconds: int = 600,
) -> bool:
    if not 30 <= lease_seconds <= 3600:
        raise ValueError("User lifecycle lease must be between 30 and 3600 seconds")
    now = datetime.now(UTC)
    async with _subscription_connection() as db:
        cursor = await db.execute(
            """UPDATE user_lifecycle_leases
               SET claimed_until = ?, updated_at = ?
               WHERE user_id = ? AND claim_token = ?""",
            (
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                now.isoformat(),
                user_id,
                claim_token,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


@asynccontextmanager
async def user_lifecycle_operation(
    user_id: str,
    operation: str,
    *,
    lease_seconds: int = 600,
) -> AsyncIterator[str | None]:
    claim_token = await claim_user_lifecycle(user_id, operation, lease_seconds=lease_seconds)
    if claim_token is None:
        yield None
        return

    stop = asyncio.Event()
    lease_lost = asyncio.Event()

    async def maintain_lease() -> None:
        interval_seconds = max(10.0, lease_seconds / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                return
            except TimeoutError:
                if not await heartbeat_user_lifecycle(user_id, claim_token, lease_seconds=lease_seconds):
                    lease_lost.set()
                    return

    heartbeat_task = asyncio.create_task(maintain_lease())
    try:
        yield claim_token
        if lease_lost.is_set():
            raise RuntimeError("User lifecycle lease was lost during the operation")
    finally:
        stop.set()
        await heartbeat_task
        if not lease_lost.is_set():
            await release_user_lifecycle(user_id, claim_token)


async def configure_subscription_registration(
    user_id: str,
    provider: str,
    endpoint: str,
    credential_key: str,
    lifecycle_claim: str = "",
) -> dict:
    """Persist desired enabled state without performing provider I/O."""
    now = datetime.now(UTC).isoformat()
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            users = await db.execute_fetchall("SELECT is_active FROM users WHERE id = ?", (user_id,))
            if not users or users[0]["is_active"] != 1:
                raise RuntimeError("Active user is required for remote subscriptions")
            await _require_no_user_lifecycle_lease(db, user_id, lifecycle_claim)
            await db.execute(
                """INSERT INTO provider_subscription_registrations
                   (user_id, provider, endpoint, credential_key, desired_state,
                    status, next_reconcile_at, last_error, updated_at)
                   VALUES (?, ?, ?, ?, 'enabled', 'pending', ?, '', ?)""",
                (user_id, provider, endpoint, credential_key, now, now),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    registration = await get_subscription_registration(user_id, provider)
    if registration is None:
        raise RuntimeError("Subscription registration was not persisted")
    return registration


async def request_subscription_disable(user_id: str, provider: str) -> dict | None:
    """Persist desired disabled state; remote resources remain until reconcile."""
    now = datetime.now(UTC).isoformat()
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        rows = await db.execute_fetchall(
            """SELECT desired_state FROM provider_subscription_registrations
               WHERE user_id = ? AND provider = ?""",
            (user_id, provider),
        )
        if rows and rows[0]["desired_state"] == "disabled":
            await db.commit()
            return await get_subscription_registration(user_id, provider)
        cursor = await db.execute(
            """UPDATE provider_subscription_registrations
               SET desired_revision = desired_revision + 1,
                   desired_state = 'disabled', status = 'disabling',
                   next_reconcile_at = ?, last_error = '', updated_at = ?
               WHERE user_id = ? AND provider = ?""",
            (now, now, user_id, provider),
        )
        await db.commit()
    if cursor.rowcount == 0:
        return None
    return await get_subscription_registration(user_id, provider)


async def get_subscription_registration(user_id: str, provider: str) -> dict | None:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM provider_subscription_registrations WHERE user_id = ? AND provider = ?",
        (user_id, provider),
    )
    return dict(rows[0]) if rows else None


async def has_subscription_registrations(user_id: str) -> bool:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT 1 FROM provider_subscription_registrations WHERE user_id = ? LIMIT 1",
        (user_id,),
    )
    return bool(rows)


async def list_due_subscription_registrations(limit: int, now: datetime | None = None) -> list[dict]:
    if not 1 <= limit <= 100:
        raise ValueError("Subscription reconcile limit must be between 1 and 100")
    db = await get_central_db()
    now_iso = (now or datetime.now(UTC)).isoformat()
    rows = await db.execute_fetchall(
        """SELECT registration.*, users.db_filename, users.email
           FROM provider_subscription_registrations AS registration
           JOIN users ON users.id = registration.user_id
           WHERE (registration.next_reconcile_at = '' OR registration.next_reconcile_at <= ?)
             AND (registration.claim_token = '' OR registration.claimed_until <= ?)
           ORDER BY registration.next_reconcile_at, registration.updated_at
           LIMIT ?""",
        (now_iso, now_iso, limit),
    )
    return [dict(row) for row in rows]


async def claim_subscription_registration(
    user_id: str,
    provider: str,
    worker_id: str,
    now: datetime | None = None,
    lease_seconds: int = 600,
) -> dict | None:
    if not worker_id or len(worker_id) > 200:
        raise ValueError("Subscription worker ID is required and bounded")
    if not 30 <= lease_seconds <= 3600:
        raise ValueError("Subscription lease must be between 30 and 3600 seconds")
    db = await get_central_db()
    claimed_at = now or datetime.now(UTC)
    claimed_at_iso = claimed_at.isoformat()
    claimed_until = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
    claim_token = uuid.uuid4().hex
    cursor = await db.execute(
        """UPDATE provider_subscription_registrations
           SET claim_token = ?, claim_owner = ?, claimed_until = ?, updated_at = ?
           WHERE user_id = ? AND provider = ?
             AND (claim_token = '' OR claimed_until <= ?)""",
        (claim_token, worker_id, claimed_until, claimed_at_iso, user_id, provider, claimed_at_iso),
    )
    await db.commit()
    if cursor.rowcount != 1:
        return None
    registration = await get_subscription_registration(user_id, provider)
    if registration is None or registration["claim_token"] != claim_token:
        raise RuntimeError("Subscription claim fencing invariant failed")
    return registration


async def heartbeat_subscription_claim(
    user_id: str,
    provider: str,
    claim_token: str,
    desired_revision: int,
    desired_state: str,
    now: datetime | None = None,
    lease_seconds: int = 600,
) -> bool:
    if desired_state not in {"enabled", "disabled"}:
        raise ValueError("Unsupported subscription desired state")
    heartbeat_at = now or datetime.now(UTC)
    claimed_until = (heartbeat_at + timedelta(seconds=lease_seconds)).isoformat()
    async with _subscription_connection() as db:
        cursor = await db.execute(
            """UPDATE provider_subscription_registrations
               SET claimed_until = ?, updated_at = ?
               WHERE user_id = ? AND provider = ? AND claim_token = ?
                 AND desired_revision = ? AND desired_state = ?""",
            (
                claimed_until,
                heartbeat_at.isoformat(),
                user_id,
                provider,
                claim_token,
                desired_revision,
                desired_state,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def release_superseded_subscription_claim(user_id: str, provider: str, claim_token: str) -> bool:
    """Release only the caller's stale claim without changing newer desired state."""
    async with _subscription_connection() as db:
        cursor = await db.execute(
            """UPDATE provider_subscription_registrations
               SET claim_token = '', claim_owner = '', claimed_until = '', updated_at = ?
               WHERE user_id = ? AND provider = ? AND claim_token = ?""",
            (datetime.now(UTC).isoformat(), user_id, provider, claim_token),
        )
        await db.commit()
        return cursor.rowcount == 1


async def publish_subscription_reconcile(
    user_id: str,
    provider: str,
    claim_token: str,
    desired_revision: int,
    *,
    endpoint: str,
    credential_key: str,
    status: str,
    renewal_due_at: str,
    next_reconcile_at: str,
    items: list[dict],
    last_error: str = "",
    now: datetime | None = None,
) -> None:
    if status not in {"active", "degraded"}:
        raise ValueError("Completed subscription status must be active or degraded")
    if len(items) > 100:
        raise ValueError("Provider subscription item count exceeds 100")
    keys = [str(item["subscription_key"]) for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError("Provider subscription keys must be unique")
    now_iso = (now or datetime.now(UTC)).isoformat()
    async with _subscription_connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            claims = await db.execute_fetchall(
                """SELECT 1 FROM provider_subscription_registrations
                   WHERE user_id = ? AND provider = ? AND claim_token = ?
                     AND desired_revision = ? AND desired_state = 'enabled'""",
                (user_id, provider, claim_token, desired_revision),
            )
            if not claims:
                raise RuntimeError("Subscription reconcile claim was superseded")
            await db.execute(
                "DELETE FROM provider_subscription_items WHERE user_id = ? AND provider = ?",
                (user_id, provider),
            )
            for item in items:
                await db.execute(
                    """INSERT INTO provider_subscription_items
                       (user_id, provider, subscription_key, remote_id, renew_at, provider_data, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        provider,
                        item["subscription_key"],
                        item["remote_id"],
                        item.get("renew_at", ""),
                        item.get("provider_data", "{}"),
                        now_iso,
                    ),
                )
            cursor = await db.execute(
                """UPDATE provider_subscription_registrations
                   SET endpoint = ?, credential_key = ?, status = ?, renewal_due_at = ?,
                       last_reconciled_at = ?, next_reconcile_at = ?, last_error = ?,
                       claim_token = '', claim_owner = '', claimed_until = '', updated_at = ?
                   WHERE user_id = ? AND provider = ? AND claim_token = ?
                     AND desired_revision = ? AND desired_state = 'enabled'""",
                (
                    endpoint,
                    credential_key,
                    status,
                    renewal_due_at,
                    now_iso,
                    next_reconcile_at,
                    last_error[:1000],
                    now_iso,
                    user_id,
                    provider,
                    claim_token,
                    desired_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Subscription reconcile claim was superseded")
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def fail_subscription_reconcile(
    user_id: str,
    provider: str,
    claim_token: str,
    desired_revision: int,
    desired_state: str,
    error: str,
    next_reconcile_at: str,
    now: datetime | None = None,
) -> bool:
    now_iso = (now or datetime.now(UTC)).isoformat()
    async with _subscription_connection() as db:
        cursor = await db.execute(
            """UPDATE provider_subscription_registrations
               SET status = 'error', last_reconciled_at = ?, next_reconcile_at = ?,
                   last_error = ?, claim_token = '', claim_owner = '', claimed_until = '', updated_at = ?
               WHERE user_id = ? AND provider = ? AND claim_token = ?
                 AND desired_revision = ? AND desired_state = ?""",
            (
                now_iso,
                next_reconcile_at,
                error[:1000],
                now_iso,
                user_id,
                provider,
                claim_token,
                desired_revision,
                desired_state,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def get_subscription_items(user_id: str, provider: str) -> list[dict]:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        """SELECT * FROM provider_subscription_items
           WHERE user_id = ? AND provider = ? ORDER BY subscription_key""",
        (user_id, provider),
    )
    return [dict(row) for row in rows]


async def delete_subscription_registration(
    user_id: str,
    provider: str,
    claim_token: str,
    desired_revision: int,
) -> None:
    async with _subscription_connection() as db:
        cursor = await db.execute(
            """DELETE FROM provider_subscription_registrations
               WHERE user_id = ? AND provider = ? AND claim_token = ?
                 AND desired_revision = ? AND desired_state = 'disabled'""",
            (user_id, provider, claim_token, desired_revision),
        )
        await db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("Subscription disable claim was superseded")


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
