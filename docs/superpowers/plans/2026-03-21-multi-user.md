# Multi-User Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert Virgil from single-user to multi-user with per-user isolated SQLite databases, central auth, signup/login, and an admin panel.

**Architecture:** Central DB (`virgil-central.db`) stores user registry. Each user gets their own SQLite file (`data/users/{uuid}.db`) with all personal data. Auth middleware reads session cookie → resolves user → opens per-user DB. All existing routers switch from `get_db()` to reading `request.state.user_db`.

**Tech Stack:** FastAPI, aiosqlite, bcrypt, itsdangerous, Jinja2

**Spec:** `docs/superpowers/specs/2026-03-21-multi-user-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/config.py` | Modify | Add `CENTRAL_DB_PATH`, `ADMIN_EMAILS`, `REGISTRATION_OPEN` |
| `app/central_db.py` | Create | Central DB connection, `users` table init, user CRUD |
| `app/user_db.py` | Create | Per-user DB creation, connection, migration runner |
| `app/auth.py` | Rewrite | Multi-user auth middleware, session = UUID |
| `app/routers/auth.py` | Rewrite | Signup + login against central DB |
| `app/routers/admin.py` | Create | Admin panel CRUD |
| `app/templates/auth_signup.html` | Create | Signup page |
| `app/templates/admin_users.html` | Create | Admin user list |
| `app/main.py` | Modify | Init central DB, register admin router, adapt feature flags |
| All 12 data routers | Modify | `get_db()` → `request.state.user_db` |
| `app/services/scheduler.py` | Modify | Iterate over all users |
| `scripts/migrate_to_multiuser.py` | Create | One-time migration for existing installs |
| `.env.example` | Modify | Add new env vars |

---

### Task 1: Config — new env vars

**Files:**
- Modify: `app/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Update config.py**

Add at the end of `/Users/krzysztofbury/PRIV/virgil/app/config.py`:

```python
# Multi-user settings.
CENTRAL_DB_PATH = os.environ.get(
    "VIRGIL_CENTRAL_DB_PATH",
    str(Path(__file__).parent.parent / "data" / "virgil-central.db"),
)
USERS_DB_DIR = str(Path(CENTRAL_DB_PATH).parent / "users")
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("VIRGIL_ADMIN_EMAILS", "").split(",")
    if e.strip()
]
REGISTRATION_OPEN = os.environ.get("VIRGIL_REGISTRATION_OPEN", "true").lower() == "true"
```

- [ ] **Step 2: Update .env.example**

Add to `/Users/krzysztofbury/PRIV/virgil/.env.example`:

```bash

# Multi-user — admin emails (comma-separated, always have admin role)
VIRGIL_ADMIN_EMAILS=admin@example.com

# Registration open/closed (default: true)
VIRGIL_REGISTRATION_OPEN=true
```

- [ ] **Step 3: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/config.py && uv run ruff format app/config.py
git add app/config.py .env.example && git commit -m "config: add multi-user env vars (admin emails, registration toggle)"
```

---

### Task 2: Central DB — user registry

**Files:**
- Create: `app/central_db.py`

- [ ] **Step 1: Create central_db.py**

Create `/Users/krzysztofbury/PRIV/virgil/app/central_db.py`:

```python
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


async def create_user(email: str, password: str, display_name: str = "") -> dict:
    """Create a new user. Returns the user dict."""
    db = await get_central_db()
    user_id = str(uuid.uuid4())
    db_filename = f"{user_id}.db"
    pw_hash = hash_password(password)

    role = "admin" if email.lower() in ADMIN_EMAILS else "user"

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
    rows = await db.execute_fetchall(
        "SELECT * FROM users WHERE email = ?", (email.lower(),)
    )
    return dict(rows[0]) if rows else None


async def get_all_users() -> list[dict]:
    """Return all users ordered by creation date."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT * FROM users ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def get_active_users() -> list[dict]:
    """Return all active users."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM users WHERE is_active = 1 ORDER BY created_at"
    )
    return [dict(r) for r in rows]


async def update_user(user_id: str, **fields) -> None:
    """Update specific fields on a user row."""
    db = await get_central_db()
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [user_id]
    await db.execute(f"UPDATE users SET {sets} WHERE id = ?", values)  # noqa: S608
    await db.commit()


async def delete_user(user_id: str) -> str | None:
    """Delete a user. Returns their db_filename for cleanup, or None."""
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT db_filename FROM users WHERE id = ?", (user_id,)
    )
    if not rows:
        return None
    db_filename = rows[0]["db_filename"]
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    return db_filename


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
```

- [ ] **Step 2: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/central_db.py && uv run ruff format app/central_db.py
git add app/central_db.py && git commit -m "feat: add central DB with user registry CRUD"
```

---

### Task 3: Per-user DB management

**Files:**
- Create: `app/user_db.py`

- [ ] **Step 1: Create user_db.py**

Create `/Users/krzysztofbury/PRIV/virgil/app/user_db.py`:

```python
"""Per-user database management — create, connect, migrate."""

import os
from pathlib import Path

import aiosqlite
from starlette.requests import Request

from app.config import USERS_DB_DIR
from app.migrations.runner import run_migrations


async def create_user_db(db_filename: str) -> None:
    """Create a new per-user database and run all migrations."""
    db_dir = Path(USERS_DB_DIR)
    db_dir.mkdir(parents=True, exist_ok=True)

    db_path = str(db_dir / db_filename)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row

    result = await db.execute_fetchall("PRAGMA journal_mode=WAL")
    assert result[0][0].lower() == "wal", f"WAL mode not enabled: {result}"
    await db.execute("PRAGMA foreign_keys=ON")

    await run_migrations(db)
    await db.close()


async def open_user_db(db_filename: str) -> aiosqlite.Connection:
    """Open a connection to an existing per-user database."""
    db_path = str(Path(USERS_DB_DIR) / db_filename)
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def close_user_db(db: aiosqlite.Connection) -> None:
    """Close a per-user database connection."""
    if db:
        await db.close()


def delete_user_db(db_filename: str) -> None:
    """Delete a per-user database file and its WAL/SHM files."""
    db_path = Path(USERS_DB_DIR) / db_filename
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix)
        if path.exists():
            os.remove(path)


def get_user_db_from_request(request: Request) -> aiosqlite.Connection:
    """Extract the per-user DB connection from request state.

    All routers should use this instead of get_db().
    """
    db = getattr(request.state, "user_db", None)
    if db is None:
        raise RuntimeError("No user_db in request state — auth middleware did not run")
    return db
```

- [ ] **Step 2: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/user_db.py && uv run ruff format app/user_db.py
git add app/user_db.py && git commit -m "feat: add per-user DB creation, connection, and lifecycle management"
```

---

### Task 4: Rewrite auth middleware

**Files:**
- Rewrite: `app/auth.py`

- [ ] **Step 1: Rewrite auth.py**

Replace the ENTIRE contents of `/Users/krzysztofbury/PRIV/virgil/app/auth.py` with:

```python
"""Authentication middleware and session utilities for multi-user Virgil."""

import logging

import bcrypt
from itsdangerous import BadSignature, TimestampSigner
from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import BASE_URL

logger = logging.getLogger(__name__)

SESSION_COOKIE = "virgil_session"
SESSION_MAX_AGE_SECONDS = 86400 * 7  # 7 days

# Paths that bypass auth entirely.
PUBLIC_PATHS = frozenset({
    "/login", "/signup", "/mfa/verify", "/offline",
    "/service-worker.js", "/api/oura/webhook",
})
PUBLIC_PREFIXES = ("/static/",)

_signer: TimestampSigner | None = None


def _get_signer() -> TimestampSigner:
    global _signer
    if _signer is None:
        from app.services.encryption import get_signing_key
        _signer = TimestampSigner(get_signing_key())
    return _signer


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("Cannot hash an empty password")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    if not password:
        raise ValueError("Cannot verify an empty password")
    if not password_hash.startswith("$2"):
        raise ValueError("Invalid bcrypt hash format")
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_session(user_id: str) -> str:
    """Create a signed session token containing the user UUID."""
    return _get_signer().sign(user_id).decode()


def validate_session(token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> str | None:
    """Validate session token, return user UUID or None."""
    try:
        return _get_signer().unsign(token, max_age=max_age).decode()
    except BadSignature:
        return None


def session_cookie_header(token: str) -> str:
    """Build Set-Cookie header value for the session."""
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Strict{secure}; Max-Age={SESSION_MAX_AGE_SECONDS}; Path=/"


def clear_session_cookie() -> str:
    """Build Set-Cookie header value that clears the session."""
    secure = "; Secure" if BASE_URL.startswith("https") else ""
    return f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict{secure}; Max-Age=0; Path=/"


def _reset_caches():
    """Reset all cached auth state — called on factory reset."""
    global _signer
    _signer = None


def mark_onboarding_complete():
    """No-op in multi-user mode — onboarding state is per-user DB."""
    pass


class AuthMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # Allow public paths.
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        # Check session cookie — contains user UUID.
        request = Request(scope)
        session_token = request.cookies.get(SESSION_COOKIE, "")
        user_id = validate_session(session_token) if session_token else None

        if not user_id or user_id.startswith("_mfa_pending:"):
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Look up user in central DB.
        from app.central_db import get_user_by_id
        user = await get_user_by_id(user_id)

        if not user or not user["is_active"]:
            response = RedirectResponse("/login", status_code=303)
            await response(scope, receive, send)
            return

        # Open per-user database connection.
        from app.user_db import close_user_db, open_user_db
        user_db = await open_user_db(user["db_filename"])

        # Store user + DB in request state.
        scope["state"] = {
            **scope.get("state", {}),
            "username": user["email"],
            "user": user,
            "user_db": user_db,
        }

        try:
            # Check onboarding for this user.
            from app.db import get_setting
            done = await get_setting(user_db, "onboarding_completed", "0")
            if done != "1" and not path.startswith(("/onboarding", "/static/", "/api/", "/logout", "/service-worker")):
                response = RedirectResponse("/onboarding", status_code=303)
                await response(scope, receive, send)
                return

            await self.app(scope, receive, send)
        finally:
            await close_user_db(user_db)
```

- [ ] **Step 2: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/auth.py && uv run ruff format app/auth.py
git add app/auth.py && git commit -m "feat: rewrite auth middleware for multi-user (UUID sessions, per-user DB)"
```

---

### Task 5: Rewrite auth router — signup + login

**Files:**
- Rewrite: `app/routers/auth.py`
- Create: `app/templates/auth_signup.html`

- [ ] **Step 1: Rewrite the auth router**

Replace the ENTIRE contents of `/Users/krzysztofbury/PRIV/virgil/app/routers/auth.py`. The new version:
- Removes `/setup` entirely
- Adds `/signup` (GET + POST)
- Rewrites `/login` to check central DB
- Keeps MFA flow but reads from central DB
- Keep the QR code endpoint

Read the current file first to preserve the MFA logic (lines 150+), then rewrite with:
- `from app.central_db import create_user, get_user_by_email, update_user` instead of `get_db()`
- Signup creates user + per-user DB via `create_user_db()`
- Login validates against central DB
- MFA reads `totp_secret`/`totp_enabled` from central `users` table

Key changes:
- `_get_user(db)` → `get_user_by_email(email)` or `get_user_by_id(user_id)` from central_db
- `/setup` routes removed, replaced with `/signup`
- Session stores UUID (not email) via `create_session(user["id"])`
- After signup: create per-user DB, log in, redirect to `/onboarding`

- [ ] **Step 2: Create signup template**

Create `/Users/krzysztofbury/PRIV/virgil/app/templates/auth_signup.html` — same structure as `auth_login.html` but with email, display name, password, confirm password fields.

- [ ] **Step 3: Update auth_login.html**

Update the login template: change any "Don't have an account?" link from `/setup` to `/signup`.

- [ ] **Step 4: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/auth.py && uv run ruff format app/routers/auth.py
git add app/routers/auth.py app/templates/auth_signup.html app/templates/auth_login.html
git commit -m "feat: rewrite auth routes — signup/login against central DB, remove /setup"
```

---

### Task 6: Admin panel

**Files:**
- Create: `app/routers/admin.py`
- Create: `app/templates/admin_users.html`

- [ ] **Step 1: Create admin router**

Create `/Users/krzysztofbury/PRIV/virgil/app/routers/admin.py`:

```python
"""Admin panel — user management."""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.central_db import delete_user, get_all_users, update_user
from app.config import ADMIN_EMAILS, REGISTRATION_OPEN
from app.main import templates
from app.user_db import delete_user_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin")


def _require_admin(request: Request) -> dict:
    """Return user dict if admin, raise 403 otherwise."""
    user = getattr(request.state, "user", None)
    if not user or user["role"] != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/users", response_class=HTMLResponse)
async def list_users(request: Request):
    _require_admin(request)
    users = await get_all_users()
    return templates.TemplateResponse("admin_users.html", {
        "request": request,
        "users": users,
        "total": len(users),
        "registration_open": REGISTRATION_OPEN,
        "admin_emails": ADMIN_EMAILS,
    })


@router.post("/users/{user_id}/disable")
async def disable_user(request: Request, user_id: str):
    _require_admin(request)
    await update_user(user_id, is_active=0)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    _require_admin(request)
    await update_user(user_id, is_active=1)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user_route(request: Request, user_id: str):
    admin = _require_admin(request)
    # Prevent self-deletion.
    if user_id == admin["id"]:
        return RedirectResponse("/admin/users", status_code=303)
    db_filename = await delete_user(user_id)
    if db_filename:
        delete_user_db(db_filename)
    return RedirectResponse("/admin/users", status_code=303)
```

- [ ] **Step 2: Create admin template**

Create `/Users/krzysztofbury/PRIV/virgil/app/templates/admin_users.html` extending base.html. Table with: email, display name, role, status (active badge / disabled badge), last login, created at, and action buttons (disable/enable/delete). Header shows total user count and registration status.

- [ ] **Step 3: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/admin.py && uv run ruff format app/routers/admin.py
git add app/routers/admin.py app/templates/admin_users.html
git commit -m "feat: add admin panel — list, disable, enable, delete users"
```

---

### Task 7: Update main.py — init central DB, register routers

**Files:**
- Modify: `app/main.py`

- [ ] **Step 1: Update lifespan**

In the `lifespan` function, add central DB init before `init_db()` and promote admin emails:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.central_db import close_central_db, init_central_db, promote_admin_emails
    await init_central_db()
    await promote_admin_emails()

    from app.services.scheduler import scheduler_loop
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await close_central_db()
```

Remove `init_db()` and `close_db()` from lifespan — per-user DBs are opened/closed per-request by middleware now.

- [ ] **Step 2: Update feature flags middleware**

The feature flags middleware currently calls `get_db()`. It needs to read from the per-user DB in request state. Change `inject_feature_flags`:

```python
@app.middleware("http")
async def inject_feature_flags(request, call_next):
    user_db = getattr(request.state, "user_db", None)
    if user_db:
        from app.db import get_feature_flags
        request.state.features = await get_feature_flags(user_db)
    else:
        request.state.features = {}
    return await call_next(request)
```

Remove the caching (`_flags_cache`) — it was global, but now flags are per-user. Per-request query is fine for SQLite.

- [ ] **Step 3: Register admin router + remove /setup import**

Add `admin` to the router imports and `app.include_router(admin.router)`. Remove `setup` references if they existed in the auth router.

- [ ] **Step 4: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/main.py && uv run ruff format app/main.py
git add app/main.py && git commit -m "feat: init central DB on startup, register admin router, per-user feature flags"
```

---

### Task 8: Mechanical replacement — get_db() → request.state.user_db

**Files:**
- Modify: All 12 data routers + `app/routers/onboarding.py`

This is the biggest task but entirely mechanical. In every router file:

1. Remove `from app.db import get_db` (keep other imports from `app.db` like `get_setting`, `set_setting`, `get_feature_flags`, constants)
2. Add `from app.user_db import get_user_db_from_request`
3. Replace every `db = await get_db()` with `db = get_user_db_from_request(request)`

**Files to modify (13 total):**
- `app/routers/dashboard.py`
- `app/routers/daily.py`
- `app/routers/training.py`
- `app/routers/feniks.py`
- `app/routers/oura.py`
- `app/routers/oura_webhook.py`
- `app/routers/bloodwork.py`
- `app/routers/life_scores.py`
- `app/routers/goals.py`
- `app/routers/experiments.py`
- `app/routers/settings.py`
- `app/routers/onboarding.py`

**Special case — `oura_webhook.py`:** The webhook endpoint is unauthenticated (CSRF-exempt, no session). It needs to look up the Oura integration by matching the webhook against the correct user. For now, since the webhook URL includes no user identifier, this handler needs to search all user DBs for the matching integration. Alternative: skip the webhook handler update and mark it as TODO.

**Special case — `settings.py`:** The factory reset handler uses `get_db()` differently — it should delete the current user's DB, not a global DB. Update to use `request.state.user` to find the DB filename.

- [ ] **Step 1: Replace get_db() in all 12 routers**

For each file, the pattern is:
```python
# Before:
from app.db import get_db
# ...
db = await get_db()

# After:
from app.user_db import get_user_db_from_request
# ...
db = get_user_db_from_request(request)
```

Note: Some handlers don't have `request` as a parameter (e.g., `oura_webhook`). Those need `request: Request` added to their signature.

- [ ] **Step 2: Lint all modified files**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/routers/ && uv run ruff format app/routers/`

- [ ] **Step 3: Commit**

```bash
git add app/routers/
git commit -m "refactor: replace get_db() with get_user_db_from_request() in all routers"
```

---

### Task 9: Update scheduler for multi-user

**Files:**
- Modify: `app/services/scheduler.py`

- [ ] **Step 1: Update scheduler to iterate over users**

The scheduler currently calls `get_db()` once. In multi-user mode it needs to iterate over all active users and run tasks per-user.

Replace the main `_check_and_run` invocation in `scheduler_loop` with:

```python
from app.central_db import get_active_users
from app.user_db import close_user_db, open_user_db

users = await get_active_users()
for user in users:
    try:
        user_db = await open_user_db(user["db_filename"])
        await _check_and_run(user_db)
        await close_user_db(user_db)
    except Exception:
        logger.exception("Scheduler failed for user %s", user["email"])
```

Remove the global `get_db()` import and usage from the scheduler.

- [ ] **Step 2: Lint + commit**

```bash
cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/services/scheduler.py && uv run ruff format app/services/scheduler.py
git add app/services/scheduler.py && git commit -m "feat: scheduler iterates over all active users"
```

---

### Task 10: Migration script + E2E verification

**Files:**
- Create: `scripts/migrate_to_multiuser.py`

- [ ] **Step 1: Create migration script**

Create `/Users/krzysztofbury/PRIV/virgil/scripts/migrate_to_multiuser.py`:

```python
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
    from app.config import ADMIN_EMAILS, CENTRAL_DB_PATH, USERS_DB_DIR
    from app.central_db import CENTRAL_SCHEMA

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
        (user_id, email, user["password_hash"], email, role, db_filename,
         user.get("totp_secret", ""), 1 if user.get("totp_enabled") else 0),
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

    print(f"Migration complete!")
    print(f"  User: {email} (role: {role})")
    print(f"  Central DB: {CENTRAL_DB_PATH}")
    print(f"  User DB: {new_path}")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Kill any running server**

Run: `lsof -ti:8123 | xargs kill -9 2>/dev/null; true`

- [ ] **Step 3: Run migration if old DB exists**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python scripts/migrate_to_multiuser.py`

- [ ] **Step 4: Start server**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run python -m app`
Expected: Starts with no errors.

- [ ] **Step 5: Test signup flow**

Navigate to http://localhost:8123/signup — create a new account. Verify:
- Account created, redirected to /onboarding
- Per-user DB created in `data/users/`

- [ ] **Step 6: Test admin panel**

Set `VIRGIL_ADMIN_EMAILS` to your email in `.env`, restart. Navigate to `/admin/users`. Verify user list shows.

- [ ] **Step 7: Final lint**

Run: `cd /Users/krzysztofbury/PRIV/virgil && uv run ruff check app/ scripts/ && uv run ruff format --check app/ scripts/`

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: complete multi-user architecture — per-user DBs, central auth, admin panel"
```
