import asyncio
import csv
import io
import json
import logging
import secrets
from datetime import UTC
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.config import BASE_URL, DB_PATH, SECOND_BRAIN_PATH
from app.db import get_feature_flags, get_setting, set_setting
from app.main import templates
from app.services.encryption import decrypt, encrypt
from app.services.oura_api import (
    create_webhook_subscription,
    ensure_valid_token,
    exchange_code,
    get_oura_auth_url,
    sync_oura_from_api,
)
from app.user_db import get_user_db_from_request

logger = logging.getLogger(__name__)

router = APIRouter()

SETTINGS_TABS = ["general", "configuration", "integrations", "data", "automation", "security"]


@router.post("/api/settings/theme")
async def save_theme(request: Request):
    data = await request.json()
    theme = data.get("theme", "dark")
    if theme not in ("dark", "light"):
        theme = "dark"
    db = get_user_db_from_request(request)
    await set_setting(db, "theme", theme)
    return Response("ok")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, tab: str = Query("general")):
    if tab not in SETTINGS_TABS:
        tab = "general"

    db = get_user_db_from_request(request)

    # Always needed for tab nav
    context: dict = {
        "request": request,
        "active_tab": tab,
        "tabs": SETTINGS_TABS,
    }

    if tab == "general":
        providers = await db.execute_fetchall("SELECT * FROM llm_providers ORDER BY created_at DESC")
        providers = [dict(row) for row in providers]
        for p in providers:
            try:
                plain = decrypt(p["api_key_enc"])
                p["api_key_masked"] = f"{plain[:4]}...{plain[-4:]}" if len(plain) > 8 else "****"
            except Exception:
                p["api_key_masked"] = "(encrypted)"
        context["db_path"] = DB_PATH
        context["second_brain_path"] = SECOND_BRAIN_PATH
        context["llm_providers"] = providers
        context["feature_flags"] = await get_feature_flags(db)

    elif tab == "configuration":
        from app.routers.training import SECTION_ORDER

        lib_rows = await db.execute_fetchall("SELECT * FROM exercise_library ORDER BY category, display_order, name")
        library_by_category: dict[str, list[dict]] = {}
        for r in lib_rows:
            library_by_category.setdefault(r["category"], []).append(dict(r))
        context["library_by_category"] = library_by_category
        context["library_categories"] = sorted(library_by_category.keys())
        context["section_order"] = SECTION_ORDER

    elif tab == "integrations":
        oura_row = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
        context["oura_integration"] = dict(oura_row[0]) if oura_row else None
        oura_sync_enabled = await get_setting(db, "oura_sync_enabled", "0")
        context["oura_sync_enabled"] = oura_sync_enabled == "1"
        # Webhook info — the callback URL carries a per-user opaque id.
        webhook_id = await get_setting(db, "oura_webhook_id", "")
        if context["oura_integration"] and context["oura_integration"].get("webhook_secret") and webhook_id:
            context["webhook_enabled"] = True
            context["webhook_url"] = f"{BASE_URL}/api/oura/webhook/{webhook_id}"
        else:
            context["webhook_enabled"] = False
            context["webhook_url"] = ""

    elif tab == "data":
        from app.services.markdown_export import export_filename_for

        context["second_brain_path"] = SECOND_BRAIN_PATH
        context["export_filename"] = await export_filename_for(db, request.state.user["id"])

    elif tab == "automation":
        context["backup_enabled"] = await get_setting(db, "backup_enabled", "1") == "1"
        context["backup_interval_hours"] = await get_setting(db, "backup_interval_hours", "24")
        context["backup_max_copies"] = await get_setting(db, "backup_max_copies", "7")
        context["oura_sync_enabled"] = await get_setting(db, "oura_sync_enabled", "0") == "1"
        context["oura_sync_interval_hours"] = await get_setting(db, "oura_sync_interval_hours", "6")
        context["briefing_enabled"] = await get_setting(db, "briefing_enabled", "0") == "1"
        context["export_enabled"] = await get_setting(db, "export_enabled", "0") == "1"
        context["export_interval_hours"] = await get_setting(db, "export_interval_hours", "6")
        # Check if oura is connected
        oura_row = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
        context["oura_connected"] = bool(oura_row and oura_row[0]["status"] == "connected")

    elif tab == "security":
        # MFA status lives in the central users table, not per-user DB.
        user = getattr(request.state, "user", {})
        context["mfa_enabled"] = bool(user.get("totp_enabled"))
        logs = await db.execute_fetchall("SELECT * FROM sync_log ORDER BY created_at DESC LIMIT 50")
        context["sync_logs"] = [dict(row) for row in logs]

    return templates.TemplateResponse("settings.html", context)


# --- App Configuration: dictionary tables ---
# Rules: users add their own rows; built-in (seeded) rows can only be
# archived/restored — never edited or deleted, so app upgrades stay clean.


@router.post("/settings/library/add")
async def library_add(
    request: Request,
    name: str = Form(...),
    category: str = Form(...),
    section: str = Form("Core"),
    sets: str = Form(""),
    reps: str = Form(""),
    notes: str = Form(""),
):
    from app.routers.training import SECTION_ORDER
    from app.validation import truncate

    if section not in SECTION_ORDER:
        section = "Core"
    name = truncate(name.strip(), 100)
    category = truncate(category.strip(), 100)
    if not name or not category:
        return RedirectResponse("/settings?tab=configuration", status_code=303)
    try:
        sets_val = max(1, min(20, int(sets))) if sets.strip() else None
    except ValueError:
        sets_val = None

    db = get_user_db_from_request(request)
    await db.execute(
        "INSERT OR IGNORE INTO exercise_library (category, section, name, sets, reps, notes, display_order, builtin) "
        "VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(display_order), 0) + 1 FROM exercise_library), 0)",
        (category, section, name, sets_val, truncate(reps.strip(), 100), truncate(notes.strip(), 300)),
    )
    await db.commit()
    return RedirectResponse("/settings?tab=configuration", status_code=303)


@router.post("/settings/library/update")
async def library_update(
    request: Request,
    entry_id: int = Form(...),
    name: str = Form(...),
    section: str = Form("Core"),
    sets: str = Form(""),
    reps: str = Form(""),
    notes: str = Form(""),
):
    from app.routers.training import SECTION_ORDER
    from app.validation import truncate

    if section not in SECTION_ORDER:
        section = "Core"
    name = truncate(name.strip(), 100)
    if not name:
        return RedirectResponse("/settings?tab=configuration", status_code=303)
    try:
        sets_val = max(1, min(20, int(sets))) if sets.strip() else None
    except ValueError:
        sets_val = None

    db = get_user_db_from_request(request)
    await db.execute(
        "UPDATE exercise_library SET name = ?, section = ?, sets = ?, reps = ?, notes = ? WHERE id = ? AND builtin = 0",
        (name, section, sets_val, truncate(reps.strip(), 100), truncate(notes.strip(), 300), entry_id),
    )
    await db.commit()
    return RedirectResponse("/settings?tab=configuration", status_code=303)


@router.post("/settings/library/delete")
async def library_delete(request: Request, entry_id: int = Form(...)):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM exercise_library WHERE id = ? AND builtin = 0", (entry_id,))
    await db.commit()
    return RedirectResponse("/settings?tab=configuration", status_code=303)


@router.post("/settings/library/archive")
async def library_archive(request: Request, entry_id: int = Form(...), archived: int = Form(1)):
    db = get_user_db_from_request(request)
    await db.execute("UPDATE exercise_library SET archived = ? WHERE id = ?", (1 if archived else 0, entry_id))
    await db.commit()
    return RedirectResponse("/settings?tab=configuration", status_code=303)


# --- Automation settings ---


@router.post("/settings/automation")
async def save_automation(request: Request):
    from app.validation import clamp_float, clamp_int

    form = await request.form()
    db = get_user_db_from_request(request)

    # Validate numeric settings before persisting to prevent scheduler crashes.
    backup_interval = clamp_float(form.get("backup_interval_hours", "24"), minimum=1.0, maximum=168.0)
    backup_max = clamp_int(form.get("backup_max_copies", "7"), minimum=1, maximum=100)
    oura_interval = clamp_float(form.get("oura_sync_interval_hours", "6"), minimum=1.0, maximum=168.0)
    export_interval = clamp_float(form.get("export_interval_hours", "6"), minimum=1.0, maximum=168.0)

    await set_setting(db, "backup_enabled", "1" if form.get("backup_enabled") else "0")
    await set_setting(db, "backup_interval_hours", str(backup_interval))
    await set_setting(db, "backup_max_copies", str(backup_max))
    await set_setting(db, "oura_sync_enabled", "1" if form.get("oura_sync_enabled") else "0")
    await set_setting(db, "oura_sync_interval_hours", str(oura_interval))
    await set_setting(db, "briefing_enabled", "1" if form.get("briefing_enabled") else "0")
    await set_setting(db, "export_enabled", "1" if form.get("export_enabled") else "0")
    await set_setting(db, "export_interval_hours", str(export_interval))

    return RedirectResponse(f"/settings?tab=automation&msg={quote('Automation settings saved')}", status_code=303)


# --- Feature Flags ---


@router.post("/settings/features")
async def save_features(request: Request):
    form = await request.form()
    db = get_user_db_from_request(request)
    # Union of flags already in the DB and any feature_* checkbox present in the form,
    # so a not-yet-seeded flag still saves instead of being silently dropped by an empty loop.
    known = set(await get_feature_flags(db))
    known |= {k.removeprefix("feature_") for k in form if k.startswith("feature_")}
    for flag_name in known:
        key = f"feature_{flag_name}"
        await set_setting(db, key, "1" if form.get(key) else "0")

    return RedirectResponse(f"/settings?tab=general&msg={quote('Features updated')}", status_code=303)


# --- Backup ---


@router.post("/settings/backup/now")
async def trigger_backup_now(request: Request):
    from app.services.backup import run_backup

    db = get_user_db_from_request(request)
    try:
        path = await run_backup(db)
        return RedirectResponse(
            f"/settings?tab=automation&msg={quote(f'Backup created: {path.name}')}",
            status_code=303,
        )
    except Exception:
        logger.exception("Manual backup failed")
        return RedirectResponse(f"/settings?tab=automation&err={quote('Backup failed')}", status_code=303)


# --- Export ---


@router.post("/settings/export")
async def trigger_export(request: Request):
    from app.services.markdown_export import export_filename_for, write_export

    form = await request.form()
    scope = form.get("scope", "weekly")
    if scope not in ("weekly", "monthly", "yearly", "all"):
        scope = "weekly"
    sections = form.getlist("sections")
    section_set = set(sections) if sections else None

    db = get_user_db_from_request(request)
    try:
        filename = await export_filename_for(db, request.state.user["id"])
        await write_export(db, scope, sections=section_set, filename=filename)
        return RedirectResponse(f"/settings?tab=data&msg={quote(f'{scope} export complete')}", status_code=303)
    except Exception:
        logger.exception("Export failed")
        return RedirectResponse(f"/settings?tab=data&err={quote('Export failed')}", status_code=303)


@router.post("/settings/import")
async def trigger_import(request: Request):
    from app.services.markdown_import import import_all

    db = get_user_db_from_request(request)
    try:
        await import_all(db)
        return RedirectResponse(f"/settings?tab=data&msg={quote('Import complete')}", status_code=303)
    except Exception:
        logger.exception("Import failed")
        return RedirectResponse(f"/settings?tab=data&err={quote('Import failed')}", status_code=303)


@router.get("/settings/backup")
async def download_backup(request: Request):
    """Download a consistent snapshot of the current user's database.

    Copies via sqlite3.backup() into a temp file so WAL contents are included —
    serving the live file directly would silently drop uncommitted -wal pages.
    """
    import asyncio
    import os
    import tempfile

    from starlette.background import BackgroundTask

    from app.services.backup import _do_backup, db_main_path

    db = get_user_db_from_request(request)
    try:
        src_path = await db_main_path(db)
        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        await asyncio.to_thread(_do_backup, src_path, tmp_path)
    except Exception:
        logger.exception("Backup download failed")
        return RedirectResponse(f"/settings?tab=data&err={quote('Backup failed')}", status_code=303)
    return FileResponse(
        tmp_path,
        filename="virgil.db",
        media_type="application/octet-stream",
        background=BackgroundTask(os.unlink, tmp_path),
    )


# Every user-owned table — credentials (llm_providers, integrations) stay out.
EXPORT_TABLES = [
    "daily_logs",
    "body_measurements",
    "daily_briefings",
    "training_sessions",
    "training_entries",
    "training_exercises",
    "exercise_library",
    "feniks_config",
    "feniks_journal",
    "feniks_pleasures",
    "feniks_milestones",
    "pmo_events",
    "oura_daily",
    "oura_monthly",
    "oura_workouts",
    "blood_markers",
    "blood_results",
    "life_scores",
    "goal_areas",
    "goals",
    "experiments",
    "experiment_activity_types",
    "experiment_weeks",
    "experiment_entries",
    "experiment_summaries",
    "user_profiles",
    "app_settings",
    "sync_log",
]


@router.get("/settings/export/json")
async def export_json(request: Request):
    db = get_user_db_from_request(request)
    data = {}
    for table in EXPORT_TABLES:
        try:
            rows = await db.execute_fetchall(f"SELECT * FROM {table}")  # noqa: S608
        except Exception:  # table missing in this user's schema — export the rest
            logger.exception("Export: table %s unreadable, skipping", table)
            data[table] = []
            continue
        data[table] = [dict(r) for r in rows]
    content = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=virgil-export.json"},
    )


@router.get("/settings/export/csv")
async def export_csv(request: Request):
    db = get_user_db_from_request(request)
    output = io.StringIO()
    for table in EXPORT_TABLES:
        try:
            rows = await db.execute_fetchall(f"SELECT * FROM {table}")  # noqa: S608
        except Exception:  # table missing in this user's schema — export the rest
            logger.exception("Export: table %s unreadable, skipping", table)
            continue
        if not rows:
            continue
        dicts = [dict(r) for r in rows]
        output.write(f"### {table}\n")
        writer = csv.DictWriter(output, fieldnames=dicts[0].keys())
        writer.writeheader()
        writer.writerows(dicts)
        output.write("\n")
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=virgil-export.csv"},
    )


# --- LLM Providers ---


@router.post("/settings/llm/add")
async def add_llm_provider(
    request: Request,
    provider: str = Form(...),
    api_key: str = Form(...),
    model: str = Form(...),
):
    from app.validation import truncate

    db = get_user_db_from_request(request)
    # Sanitize inputs — provider and model are stored as-is for LiteLLM.
    provider = truncate(provider.strip(), 50)
    model = truncate(model.strip(), 200)
    if not provider or not model or not api_key:
        return RedirectResponse("/settings?tab=general&err=All+fields+required", status_code=303)
    await db.execute(
        "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 0)",
        (provider, encrypt(api_key), model),
    )
    await db.commit()
    return RedirectResponse("/settings?tab=general", status_code=303)


@router.post("/settings/llm/activate")
async def activate_llm_provider(request: Request, provider_id: int = Form(...)):
    db = get_user_db_from_request(request)
    await db.execute("UPDATE llm_providers SET is_active = 0")
    await db.execute("UPDATE llm_providers SET is_active = 1 WHERE id = ?", (provider_id,))
    await db.commit()
    return RedirectResponse("/settings?tab=general", status_code=303)


@router.post("/settings/llm/delete")
async def delete_llm_provider(request: Request, provider_id: int = Form(...)):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM llm_providers WHERE id = ?", (provider_id,))
    await db.commit()
    return RedirectResponse("/settings?tab=general", status_code=303)


# --- Factory Reset ---


@router.post("/settings/factory-reset")
async def factory_reset(request: Request):
    """Wipe the current user's data and restart onboarding.

    The account (central registry row) and session are kept. The fresh database
    gets a NEW filename and the registry is repointed before the old file is
    deleted — recreating at the same path would race any connection still open
    on the old file (this request's own, or the scheduler mid-backup): SQLite
    unlinks `<path>-wal` by name on last close, which could destroy the new
    database's WAL. Oura webhook subscriptions are torn down first, while the
    credentials still exist to authorize the deletion.
    """
    import uuid

    from app.central_db import delete_webhook_routes, update_user
    from app.user_db import create_user_db, delete_user_db

    user = getattr(request.state, "user", None)
    if not user or not user.get("db_filename"):
        return RedirectResponse("/login", status_code=303)

    db = get_user_db_from_request(request)
    async with _oura_webhook_lock:
        await _reconcile_oura_subscriptions(db)
        await delete_webhook_routes(user["id"])

    old_filename = user["db_filename"]
    new_filename = f"{uuid.uuid4()}.db"
    await create_user_db(new_filename)
    await update_user(user["id"], db_filename=new_filename)
    delete_user_db(old_filename)

    logger.info("Factory reset completed for user %s", user["email"])
    return RedirectResponse("/onboarding", status_code=303)


# --- Oura Integration ---


@router.post("/settings/oura/save")
async def save_oura_credentials(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    db = get_user_db_from_request(request)
    await db.execute(
        """INSERT INTO integrations (provider, client_id, client_secret_enc, scopes, status)
        VALUES ('oura', ?, ?, ?, 'configured')
        ON CONFLICT(provider) DO UPDATE SET
            client_id=excluded.client_id, client_secret_enc=excluded.client_secret_enc,
            scopes=excluded.scopes, status='configured',
            access_token_enc='', refresh_token_enc='', token_expires_at=''""",
        (client_id, encrypt(client_secret), "daily heartrate session spo2 sleep workout"),
    )
    await db.commit()
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@router.get("/settings/oura/connect")
async def oura_connect(request: Request):
    db = get_user_db_from_request(request)
    row = await db.execute_fetchall("SELECT client_id FROM integrations WHERE provider = 'oura'")
    if not row:
        return RedirectResponse("/settings?tab=integrations", status_code=303)
    client_id = row[0]["client_id"]
    redirect_uri = f"{BASE_URL}/settings/oura/callback"
    state = secrets.token_urlsafe(32)
    auth_url = get_oura_auth_url(client_id, redirect_uri, state=state)
    response = RedirectResponse(auth_url, status_code=302)
    response.set_cookie(
        "oura_oauth_state",
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=BASE_URL.startswith("https"),
    )
    return response


@router.get("/settings/oura/callback")
async def oura_callback(request: Request, code: str = Query(...), state: str = Query("")):
    expected_state = request.cookies.get("oura_oauth_state", "")
    if not state or not expected_state or state != expected_state:
        logger.warning("OAuth state mismatch — possible CSRF attempt")
        return RedirectResponse("/settings?tab=integrations", status_code=303)

    db = get_user_db_from_request(request)
    row = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
    if not row:
        return RedirectResponse("/settings?tab=integrations", status_code=303)
    integration = dict(row[0])
    client_id = integration["client_id"]
    client_secret = decrypt(integration["client_secret_enc"])
    redirect_uri = f"{BASE_URL}/settings/oura/callback"

    try:
        tokens = await exchange_code(client_id, client_secret, code, redirect_uri)
        access_token = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in = tokens.get("expires_in", 86400)

        from datetime import datetime, timedelta

        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()

        await db.execute(
            """UPDATE integrations SET access_token_enc = ?, refresh_token_enc = ?,
               token_expires_at = ?, status = 'connected',
               scopes = 'daily heartrate session spo2 sleep workout'
               WHERE provider = 'oura'""",
            (encrypt(access_token), encrypt(refresh_token), expires_at),
        )
        await db.commit()
    except Exception:
        logger.exception("Oura OAuth callback failed")
        await db.execute("UPDATE integrations SET status = 'error' WHERE provider = 'oura'")
        await db.commit()

    response = RedirectResponse("/settings?tab=integrations", status_code=303)
    response.delete_cookie("oura_oauth_state")
    return response


@router.post("/settings/oura/disconnect")
async def oura_disconnect(request: Request):
    db = get_user_db_from_request(request)
    await db.execute(
        """UPDATE integrations SET access_token_enc = '', refresh_token_enc = '',
           token_expires_at = '', status = 'configured' WHERE provider = 'oura'"""
    )
    await db.commit()
    return RedirectResponse("/settings?tab=integrations", status_code=303)


@router.post("/settings/oura/sync")
async def oura_sync(request: Request):
    db = get_user_db_from_request(request)
    try:
        count = await sync_oura_from_api(db)
        logger.info("Oura sync completed: %d days", count)
        return RedirectResponse(
            f"/settings?tab=integrations&msg={quote(f'Oura sync: {count} days')}",
            status_code=303,
        )
    except Exception:
        logger.exception("Oura sync failed")
        return RedirectResponse(f"/settings?tab=integrations&err={quote('Oura sync failed')}", status_code=303)


# --- Oura Webhook ---


async def _oura_client_credentials(db) -> tuple[str, str] | None:
    """(client_id, client_secret) for the user's Oura OAuth app, or None.

    Webhook subscription management authenticates with these app credentials
    (x-client-id / x-client-secret), not the user's Bearer token.
    """
    rows = await db.execute_fetchall("SELECT client_id, client_secret_enc FROM integrations WHERE provider = 'oura'")
    if not rows or not rows[0]["client_id"] or not rows[0]["client_secret_enc"]:
        return None
    return rows[0]["client_id"], decrypt(rows[0]["client_secret_enc"])


# Serializes every reconcile + enable across users. Without it, user A's
# reconcile can snapshot known_ids, then user B enables concurrently — B's
# fresh id is missing from A's stale snapshot and gets deleted as an orphan.
# Single-process app, so one event-loop lock suffices.
_oura_webhook_lock = asyncio.Lock()


async def _reconcile_oura_subscriptions(db) -> None:
    """Best-effort removal of THIS USER'S stale subscriptions from Oura.

    Covers the user's current/previous webhook id, the legacy endpoint, and
    orphaned ids no user owns. Other users' callbacks are left alone — several
    users may share one Oura OAuth app, and a blanket wipe of every
    subscription on this deployment would silently kill their sync.
    Callers must hold _oura_webhook_lock.
    """
    assert _oura_webhook_lock.locked(), "reconcile requires _oura_webhook_lock"
    creds = await _oura_client_credentials(db)
    if not creds:
        return
    client_id, client_secret = creds
    try:
        from app.central_db import get_all_webhook_ids
        from app.services.oura_api import delete_stale_subscriptions

        own_id = await get_setting(db, "oura_webhook_id", "")
        own_ids = {own_id} if own_id else set()
        known_ids = await get_all_webhook_ids()
        removed = await delete_stale_subscriptions(client_id, client_secret, BASE_URL, own_ids, known_ids)
        if removed:
            logger.info("Removed %d stale Oura webhook subscription(s)", removed)
    except Exception:
        logger.exception("Failed to reconcile Oura webhook subscriptions (continuing anyway)")


@router.post("/settings/oura/webhook/enable")
async def enable_oura_webhook(request: Request):
    from app.central_db import create_webhook_route, delete_webhook_routes

    db = get_user_db_from_request(request)
    user = request.state.user
    # Events can only be synced with a live token, so require a connected
    # integration even though subscription management uses app credentials.
    token = await ensure_valid_token(db)
    creds = await _oura_client_credentials(db)
    if not token or not creds:
        return RedirectResponse(
            f"/settings?tab=integrations&err={quote('Oura not connected or token expired')}",
            status_code=303,
        )
    client_id, client_secret = creds

    # The lock spans reconcile AND registration: another user's concurrent
    # enable must not slip a fresh id between our known_ids snapshot and the
    # orphan deletions.
    async with _oura_webhook_lock:
        # Reconcile first: leftovers from earlier attempts (or the legacy
        # endpoint) keep delivering to dead callbacks and can conflict with
        # re-registration.
        await _reconcile_oura_subscriptions(db)

        # Per-user callback URL: the opaque id routes the public webhook to
        # this user's database (see app/routers/oura_webhook.py).
        verification_token = secrets.token_urlsafe(32)
        webhook_id = await create_webhook_route(user["id"])
        callback_url = f"{BASE_URL}/api/oura/webhook/{webhook_id}"

        # Store the secret (encrypted) first so the verification challenge can match it
        await db.execute(
            "UPDATE integrations SET webhook_secret = ? WHERE provider = 'oura'",
            (encrypt(verification_token),),
        )
        await set_setting(db, "oura_webhook_id", webhook_id)

        try:
            result = await create_webhook_subscription(client_id, client_secret, callback_url, verification_token)
            logger.info(
                "Oura webhook subscriptions created: %d ok, %d failed",
                len(result["created"]),
                len(result["failed"]),
            )
            if result["failed"]:
                # Partial coverage is a degraded state the user must see — the
                # missing data types will silently never push events.
                failed_types = ", ".join(sorted({data_type for _, data_type, _ in result["failed"]}))
                return RedirectResponse(
                    f"/settings?tab=integrations&err={quote(f'Webhook partially enabled — no events for: {failed_types}. Disable and retry for full coverage.')}",
                    status_code=303,
                )
            return RedirectResponse(
                f"/settings?tab=integrations&msg={quote('Webhook enabled')}",
                status_code=303,
            )
        except Exception:
            logger.exception("Failed to create Oura webhook subscription")
            # Roll back local state since no subscription exists
            await db.execute("UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'")
            await set_setting(db, "oura_webhook_id", "")
            await delete_webhook_routes(user["id"])
            return RedirectResponse(
                f"/settings?tab=integrations&err={quote('Failed to register webhook with Oura')}",
                status_code=303,
            )


@router.post("/settings/oura/webhook/disable")
async def disable_oura_webhook(request: Request):
    from app.central_db import delete_webhook_routes

    db = get_user_db_from_request(request)
    user = request.state.user

    async with _oura_webhook_lock:
        await _reconcile_oura_subscriptions(db)

        await db.execute("UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'")
        await set_setting(db, "oura_webhook_id", "")
        await delete_webhook_routes(user["id"])
    return RedirectResponse(
        f"/settings?tab=integrations&msg={quote('Webhook disabled')}",
        status_code=303,
    )
