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
    delete_webhook_subscription,
    ensure_valid_token,
    exchange_code,
    get_oura_auth_url,
    list_webhook_subscriptions,
    sync_oura_from_api,
)
from app.user_db import get_user_db_from_request

logger = logging.getLogger(__name__)

router = APIRouter()

SETTINGS_TABS = ["general", "integrations", "data", "automation", "security"]


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

    elif tab == "integrations":
        oura_row = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
        context["oura_integration"] = dict(oura_row[0]) if oura_row else None
        oura_sync_enabled = await get_setting(db, "oura_sync_enabled", "0")
        context["oura_sync_enabled"] = oura_sync_enabled == "1"
        # Webhook info
        if context["oura_integration"] and context["oura_integration"].get("webhook_secret"):
            context["webhook_enabled"] = True
            context["webhook_url"] = f"{BASE_URL}/api/oura/webhook"
        else:
            context["webhook_enabled"] = False
            context["webhook_url"] = ""

    elif tab == "data":
        context["second_brain_path"] = SECOND_BRAIN_PATH

    elif tab == "automation":
        context["backup_enabled"] = await get_setting(db, "backup_enabled", "0") == "1"
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
    from app.services.markdown_export import write_export

    form = await request.form()
    scope = form.get("scope", "weekly")
    if scope not in ("weekly", "monthly", "yearly", "all"):
        scope = "weekly"
    sections = form.getlist("sections")
    section_set = set(sections) if sections else None

    db = get_user_db_from_request(request)
    try:
        await write_export(db, scope, sections=section_set)
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


EXPORT_TABLES = [
    "daily_logs",
    "body_measurements",
    "training_sessions",
    "training_entries",
    "training_exercises",
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
    "experiment_entries",
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
    """Delete the current user's database and redirect to /setup for a fresh start."""
    import os

    from app.user_db import delete_user_db

    user = getattr(request.state, "user", None)
    if user and user.get("db_filename"):
        delete_user_db(user["db_filename"])
    else:
        # Fallback: remove legacy DB_PATH if present
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        for suffix in ("-wal", "-shm"):
            wal_path = DB_PATH + suffix
            if os.path.exists(wal_path):
                os.remove(wal_path)

    # Reset cached state so middleware redirects to /setup.
    from app.auth import _reset_caches

    _reset_caches()

    return RedirectResponse("/setup", status_code=303)


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
    response.set_cookie("oura_oauth_state", state, max_age=600, httponly=True, samesite="lax")
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


@router.post("/settings/oura/webhook/enable")
async def enable_oura_webhook(request: Request):
    db = get_user_db_from_request(request)
    token = await ensure_valid_token(db)
    if not token:
        return RedirectResponse(
            f"/settings?tab=integrations&err={quote('Oura not connected or token expired')}",
            status_code=303,
        )

    verification_token = secrets.token_urlsafe(32)
    callback_url = f"{BASE_URL}/api/oura/webhook"

    # Store the secret first so the verification callback can match it
    await db.execute(
        "UPDATE integrations SET webhook_secret = ? WHERE provider = 'oura'",
        (verification_token,),
    )
    await db.commit()

    try:
        result = await create_webhook_subscription(token, callback_url, verification_token)
        sub_id = result.get("id", "")
        logger.info("Oura webhook subscription created: %s", sub_id)
        return RedirectResponse(
            f"/settings?tab=integrations&msg={quote('Webhook enabled')}",
            status_code=303,
        )
    except Exception:
        logger.exception("Failed to create Oura webhook subscription")
        # Clear the secret since subscription failed
        await db.execute(
            "UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'",
        )
        await db.commit()
        return RedirectResponse(
            f"/settings?tab=integrations&err={quote('Failed to register webhook with Oura')}",
            status_code=303,
        )


@router.post("/settings/oura/webhook/disable")
async def disable_oura_webhook(request: Request):
    db = get_user_db_from_request(request)
    token = await ensure_valid_token(db)

    # Try to delete subscriptions from Oura
    if token:
        try:
            subs = await list_webhook_subscriptions(token)
            callback_url = f"{BASE_URL}/api/oura/webhook"
            for sub in subs if isinstance(subs, list) else []:
                if sub.get("callback_url") == callback_url:
                    await delete_webhook_subscription(token, str(sub["id"]))
                    logger.info("Deleted Oura webhook subscription: %s", sub["id"])
        except Exception:
            logger.exception("Failed to delete Oura webhook subscriptions (clearing local state anyway)")

    await db.execute(
        "UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'",
    )
    await db.commit()
    return RedirectResponse(
        f"/settings?tab=integrations&msg={quote('Webhook disabled')}",
        status_code=303,
    )
