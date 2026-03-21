# Multi-User Architecture Design

## Problem

Virgil is a single-user self-hosted app. The `/setup` page only works when no user exists — a second person cannot use the same instance. To grow Virgil beyond a personal tool, we need multi-user support with per-user data isolation, central authentication, and an admin panel.

## Solution

Add a central authentication database alongside per-user isolated SQLite databases. Each user gets their own database file. A central `users` table handles auth, and an admin panel allows user management. The existing Virgil app code stays single-user internally — it just connects to a different DB file per request.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Auth service location | Built into Virgil (same process) | Simple deployment, one container on QNAP |
| Per-user DB layout | `data/users/{uuid}.db` flat directory | Simple, UUID prevents enumeration |
| Admin role | `.env` super-admins + promotable admins | Break-glass recovery via env, day-to-day via panel |
| Admin panel scope | List, disable, delete users | YAGNI — invite codes, impersonation, stats are future |
| Session mechanism | Signed cookie (itsdangerous) | Matches current approach, no JWT overhead |
| Registration control | `VIRGIL_REGISTRATION_OPEN` env var | Simple toggle, default open |
| Connection management | Per-request open/close | SQLite opens in <1ms, avoids stale connection issues |

## Architecture

```
data/
├── virgil-central.db          ← User registry, auth (shared)
└── users/
    ├── a3f5c2e1-...-.db       ← User A's data (all Virgil tables)
    ├── b7d8f1a4-...-.db       ← User B's data
    └── ...
```

### Central Database (`virgil-central.db`)

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,              -- UUID
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,       -- bcrypt
    display_name TEXT,
    role TEXT DEFAULT 'user',          -- 'user' or 'admin'
    db_filename TEXT NOT NULL,         -- {uuid}.db
    is_active INTEGER DEFAULT 1,      -- admin can disable
    totp_secret TEXT,
    totp_enabled INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    last_login_at TEXT
);
```

### Per-User Database

The existing Virgil schema (migrations 001-008) minus the `auth_users` table. Contains: `daily_logs`, `training_sessions`, `goals`, `user_profiles`, `oura_daily`, `experiments`, `llm_providers`, `app_settings`, etc.

## Auth Flow

### Signup (`/signup`)
1. User enters email, password, display name
2. Create row in central `users` table (UUID generated, bcrypt hash)
3. Create `data/users/{uuid}.db`, run all migrations
4. Log user in (set session cookie with UUID)
5. Redirect to `/onboarding`

### Login (`/login`)
1. Verify email + password against central DB
2. Check `is_active == 1` (admin hasn't disabled the account)
3. Set signed session cookie containing UUID
4. Update `last_login_at`
5. Redirect to `/` (middleware handles onboarding redirect if needed)

### Per-Request Auth (Middleware)
1. Read UUID from signed session cookie
2. Look up user in central DB
3. If not found or `is_active == 0`, clear cookie, redirect to `/login`
4. Open connection to `data/users/{user.db_filename}`
5. Set `request.state.user` (user dict from central DB)
6. Set `request.state.user_db` (aiosqlite connection to per-user DB)
7. All downstream code uses `request.state.user_db` instead of `get_db()`

### Logout (`/logout`)
Clear session cookie, redirect to `/login`.

## Admin

### Super-Admin (`.env`)
```bash
VIRGIL_ADMIN_EMAILS=admin@example.com,other@example.com
```
On startup + on signup, if a user's email matches this list, their role is set to `admin`. Cannot be demoted via the panel.

### Registration Control
```bash
VIRGIL_REGISTRATION_OPEN=true   # default: anyone can sign up
VIRGIL_REGISTRATION_OPEN=false  # signup page shows "Registration closed"
```

### Admin Panel (`/admin/users`)
Only accessible to `role='admin'` users. Shows:
- User table: email, display name, role, status, last login, created at
- Actions: disable/enable toggle, delete (confirms, deletes user + DB file)
- Header: total user count, registration status

### Routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/admin/users` | List all users |
| POST | `/admin/users/{id}/disable` | Disable user account |
| POST | `/admin/users/{id}/enable` | Enable user account |
| POST | `/admin/users/{id}/delete` | Delete user + their DB |

## Per-User DB Lifecycle

### Creation (on signup)
1. Generate UUID
2. Create `data/users/{uuid}.db`
3. Run migration runner against the new DB (all migrations 001-008, skipping auth_users creation)
4. Insert `onboarding_completed = '0'` in per-user `app_settings`

### Deletion (admin action)
1. Delete user row from central DB
2. Delete `data/users/{uuid}.db` (+ WAL/SHM files)
3. Active sessions fail on next request → redirect to login

### Migration for existing single-user installs
Script: `scripts/migrate_to_multiuser.py`
1. Create `data/virgil-central.db` with `users` table
2. Read `auth_users` from `data/virgil.db`
3. Generate UUID, create user row in central DB
4. `mkdir -p data/users/`, move `data/virgil.db` → `data/users/{uuid}.db`
5. Drop `auth_users` table from the moved DB
6. If email matches `VIRGIL_ADMIN_EMAILS`, set role to `admin`

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `app/central_db.py` | Central DB connection, user CRUD, `get_central_db()` |
| `app/user_db.py` | Per-user DB connection, `get_user_db(request)`, DB creation |
| `app/routers/signup.py` | `/signup` GET/POST |
| `app/routers/admin.py` | `/admin/users` CRUD |
| `app/templates/auth_signup.html` | Signup page |
| `app/templates/admin_users.html` | Admin user list |
| `app/migrations/009_remove_auth_users.py` | Drop `auth_users` from per-user DBs |
| `scripts/migrate_to_multiuser.py` | One-time migration for existing installs |

### Modified files

| File | Change |
|------|--------|
| `app/auth.py` | Rewrite — session reads UUID from central DB, resolves per-user DB |
| `app/db.py` | Remove `auth_users` from schema constant, keep as per-user DB utilities |
| `app/routers/auth.py` | Rewrite — login/logout against central DB, remove `/setup` |
| `app/config.py` | Add `VIRGIL_ADMIN_EMAILS`, `VIRGIL_REGISTRATION_OPEN`, `CENTRAL_DB_PATH` |
| `app/main.py` | Register new routers, init central DB on startup |
| All routers (12 files) | `get_db()` → `get_user_db(request)` — mechanical find-and-replace |
| `app/services/*.py` (6 files) | Accept `db` parameter (already do) — callers pass `user_db` |
| `.env.example` | Add new env vars |
| `docker-compose.yml` | No structural changes, volume already covers `data/` |

### Unchanged
- All templates except auth pages — zero changes
- Frontend JS/CSS — zero changes
- LLM service (`app/services/llm.py`) — already takes `db` as parameter
- Encryption service — same per-user key derivation
- Scheduler — needs adaptation to run per-user (iterate over active user DBs)

## Environment Variables

```bash
# Admin emails (comma-separated) — these users are always admin
VIRGIL_ADMIN_EMAILS=admin@example.com

# Registration open/closed
VIRGIL_REGISTRATION_OPEN=true

# Central DB path (default: alongside user DBs)
VIRGIL_CENTRAL_DB_PATH=./data/virgil-central.db
```

## Scheduler Adaptation

The background scheduler currently runs against the single global DB. In multi-user mode it needs to iterate over all active users and run per-user tasks (backup, Oura sync, export). This is a simple loop:

```python
for user in get_all_active_users(central_db):
    user_db = open_user_db(user.db_filename)
    await check_and_run(user_db)
    await user_db.close()
```

## What Doesn't Change

- Per-user data queries — same SQL, just a different DB connection
- Onboarding wizard — works identically, reads/writes per-user DB
- All feature pages (daily, training, goals, etc.) — same templates, same routes
- LLM integration — already parameterized with `db` argument
- Docker deployment — same single container, same volume mount
