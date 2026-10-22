import asyncio
import contextlib
import csv
import io
import json
import logging
import secrets
from datetime import UTC, date
from urllib.parse import quote

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from app.config import BASE_URL
from app.db import get_feature_flags, get_setting, set_setting
from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.backup import backup_status
from app.services.encryption import decrypt, encrypt
from app.services.llm import REASONING_EFFORT_SETTING, REASONING_EFFORTS, resolve_reasoning_effort
from app.services.oura_api import (
    ensure_valid_token,
    exchange_code,
    get_oura_auth_url,
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
async def settings_page(request: Request, tab: str = Query("general"), job_id: int | None = Query(None, ge=1)):
    if tab not in SETTINGS_TABS:
        tab = "general"

    db = get_user_db_from_request(request)

    # Always needed for tab nav
    context: dict = {
        "request": request,
        "active_tab": tab,
        "tabs": SETTINGS_TABS,
        "job_nonce": secrets.token_hex(16),
    }

    from app.routers.jobs import current_job_view

    if tab != "automation":
        context["current_job"] = await current_job_view(db, job_id)

    if tab == "general":
        providers = await db.execute_fetchall("SELECT * FROM llm_providers ORDER BY created_at DESC")
        providers = [dict(row) for row in providers]
        for p in providers:
            try:
                plain = decrypt(p["api_key_enc"])
                p["api_key_masked"] = f"{plain[:4]}...{plain[-4:]}" if len(plain) > 8 else "****"
            except Exception:
                p["api_key_masked"] = "(encrypted)"
        from app.services.backup import db_main_path

        context["db_path"] = await db_main_path(db)
        context["llm_providers"] = providers
        context["feature_flags"] = await get_feature_flags(db)
        context["freedom_choices"] = await _freedom_choices(db)
        context["freedom_on_dashboard"] = await get_setting(db, "freedom_on_dashboard", "1") == "1"
        context["reasoning_efforts"] = list(REASONING_EFFORTS)
        context["reasoning_effort"] = await resolve_reasoning_effort(db)

    elif tab == "configuration":
        from app.library_validation import LIBRARY_SECTIONS
        from app.services.training_schedule import (
            DEFAULT_DAYS,
            DEFAULT_SWIM,
            SETTING_DAYS,
            SETTING_SWIM,
            normalize_days,
        )

        # Grouped by section (not category, which migration 019 removed).
        # LIBRARY_SECTIONS is the canonical four; training.py used to keep a
        # second copy of the same list, which this now reads instead.
        # Tags live in exercise_library_tags — one batched query rather than
        # one per row, same rationale as api.py's _tags_by_library_id.
        lib_rows = await db.execute_fetchall("SELECT * FROM exercise_library ORDER BY section, display_order, name")
        tag_rows = await db.execute_fetchall("SELECT library_id, tag FROM exercise_library_tags ORDER BY tag")
        tags_by_id: dict[int, list[str]] = {}
        for tr in tag_rows:
            tags_by_id.setdefault(tr["library_id"], []).append(tr["tag"])

        library_by_section: dict[str, list[dict]] = {s: [] for s in LIBRARY_SECTIONS}
        for r in lib_rows:
            entry = dict(r)
            entry["tags"] = tags_by_id.get(entry["id"], [])
            library_by_section.setdefault(entry["section"], []).append(entry)
        context["library_by_section"] = library_by_section
        context["section_order"] = list(LIBRARY_SECTIONS)
        context["training_days"] = await get_setting(db, SETTING_DAYS, DEFAULT_DAYS)
        context["training_day_keys"] = normalize_days(context["training_days"])
        context["training_swim_per_week"] = await get_setting(db, SETTING_SWIM, DEFAULT_SWIM)
        context["library_all_tags"] = sorted({tr["tag"] for tr in tag_rows})

    elif tab == "integrations":
        from app.central_db import get_subscription_registration

        oura_row = await db.execute_fetchall("SELECT * FROM integrations WHERE provider = 'oura'")
        context["oura_integration"] = dict(oura_row[0]) if oura_row else None
        oura_sync_enabled = await get_setting(db, "oura_sync_enabled", "0")
        context["oura_sync_enabled"] = oura_sync_enabled == "1"
        registration = await get_subscription_registration(request.state.user["id"], "oura")
        context["webhook_registration"] = registration
        context["webhook_enabled"] = registration is not None
        context["webhook_url"] = registration["endpoint"] if registration else ""
        # What Oura's application form asks for; connecting fails without it.
        context["oura_redirect_uri"] = f"{BASE_URL}/settings/oura/callback"

    elif tab == "data":
        pass

    elif tab == "automation":
        from app.routers.jobs import build_job_view
        from app.services.jobs import list_recent_job_statuses

        context["backup_enabled"] = await get_setting(db, "backup_enabled", "1") == "1"
        context["backup_interval_hours"] = await get_setting(db, "backup_interval_hours", "24")
        context["backup_max_copies"] = await get_setting(db, "backup_max_copies", "7")
        context["backup_status"] = await backup_status(db)
        user = getattr(request.state, "user", None) or {}
        if user.get("role") == "admin":
            from app.services.offsite import offsite_status

            # The whole instance's copies: an operator's concern, not each account's.
            context["offsite"] = await asyncio.to_thread(offsite_status)
        context["oura_sync_enabled"] = await get_setting(db, "oura_sync_enabled", "0") == "1"
        context["oura_sync_interval_hours"] = await get_setting(db, "oura_sync_interval_hours", "6")
        context["briefing_enabled"] = await get_setting(db, "briefing_enabled", "0") == "1"
        # Check if oura is connected
        oura_row = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
        context["oura_connected"] = bool(oura_row and oura_row[0]["status"] == "connected")
        context["recent_jobs"] = [build_job_view(row) for row in await list_recent_job_statuses(db)]

    elif tab == "security":
        # MFA status lives in the central users table, not per-user DB.
        user = getattr(request.state, "user", {})
        context["mfa_enabled"] = bool(user.get("totp_enabled"))
        # The reset deletes the server's backups, so the card says what is lost.
        context["backup_status"] = await backup_status(db)
        logs = await db.execute_fetchall("SELECT * FROM sync_log ORDER BY created_at DESC LIMIT 50")
        context["sync_logs"] = [dict(row) for row in logs]

    return templates.TemplateResponse(request, "settings.html", context)


# --- App Configuration: dictionary tables ---
# Rules: users add their own rows; built-in (seeded) rows can only be
# archived/restored — never edited or deleted, so app upgrades stay clean.


@router.post("/settings/library/add")
async def library_add(
    request: Request,
    name: str = Form(...),
    section: str = Form("Core"),
    sets: str = Form(""),
    reps: str = Form(""),
    notes: str = Form(""),
    metric: str = Form("reps"),
    tags: str = Form(""),
):
    from app.library_validation import LibraryWriteError
    from app.services.library import write_library

    try:
        sets_val = int(sets) if sets.strip() else None
    except ValueError:
        return error_redirect(request, "/settings?tab=configuration", "Sets must be a whole number.")
    try:
        await write_library(
            get_user_db_from_request(request),
            op="create",
            tags=tags,
            fields={"name": name, "section": section, "sets": sets_val, "reps": reps, "notes": notes, "metric": metric},
        )
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)
    return success_redirect(request, "/settings?tab=configuration", "Exercise added to the library.")


@router.post("/settings/library/update")
async def library_update(
    request: Request,
    entry_id: int = Form(...),
    name: str | None = Form(None),
    section: str | None = Form(None),
    sets: str | None = Form(None),
    reps: str | None = Form(None),
    notes: str | None = Form(None),
    metric: str | None = Form(None),
    tags: str | None = Form(None),
):
    from app.library_validation import LibraryWriteError
    from app.services.library import write_library

    # FastAPI treats empty optional form strings as absent. The write contract
    # distinguishes absent (preserve) from an explicit blank (clear).
    form = await request.form()
    if "tags" in form and form["tags"] == "":
        tags = ""
    fields = {
        key: value
        for key, value in {
            "name": name,
            "section": section,
            "reps": reps,
            "notes": notes,
            "metric": metric,
        }.items()
        if value is not None
    }
    for key in ("name", "section", "reps", "notes", "metric"):
        if key in form and form[key] == "":
            fields[key] = ""
    if "sets" in form and form["sets"] == "":
        sets = ""
    if sets is not None:
        try:
            fields["sets"] = int(sets) if sets.strip() else None
        except ValueError:
            return error_redirect(request, "/settings?tab=configuration", "Sets must be a whole number.")
    try:
        result = await write_library(
            get_user_db_from_request(request), op="update", entry_id=entry_id, fields=fields, tags=tags
        )
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)
    if not result["updated"]:
        return error_redirect(request, "/settings?tab=configuration", "No library changes were submitted.")
    return success_redirect(request, "/settings?tab=configuration", "Library entry updated.")


@router.post("/settings/library/delete")
async def library_delete(request: Request, entry_id: int = Form(...)):
    from app.library_validation import LibraryWriteError
    from app.services.library import write_library

    try:
        await write_library(get_user_db_from_request(request), op="delete", entry_id=entry_id)
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)
    return success_redirect(request, "/settings?tab=configuration", "Exercise deleted from the library.")


@router.post("/settings/library/archive")
async def library_archive(request: Request, entry_id: int = Form(...), archived: int = Form(1)):
    from app.library_validation import LibraryWriteError
    from app.services.library import write_library

    try:
        await write_library(
            get_user_db_from_request(request), op="archive", entry_id=entry_id, fields={"archived": archived}
        )
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)
    action = "archived" if archived else "restored"
    return success_redirect(request, "/settings?tab=configuration", f"Exercise {action}.")


# --- Training schedule ---


@router.post("/settings/training-schedule")
async def save_training_schedule(request: Request):
    """Persist the weekly schedule the A.N.D.Y. planner reads.

    Both values are normalised before storage rather than on read, so whatever
    reaches app_settings is already the canonical form and the planner cannot be
    handed a half-parsed day list.
    """
    from app.services.training_schedule import (
        SETTING_DAYS,
        SETTING_SWIM,
        SWIM_PER_WEEK_MAX,
        normalize_days,
    )
    from app.validation import truncate

    def reject(message: str) -> Response:
        return error_redirect(request, "/settings?tab=configuration", message)

    form = await request.form()
    db = get_user_db_from_request(request)

    # Both fields are validated before either is written. Two set_setting calls
    # each commit separately (app/db.py), so there is no transaction to roll
    # back — a half-valid submission must be refused before the first write,
    # not repaired after it.
    # The page sends one checkbox per day; a comma list in training_days, the
    # form's earlier shape, is still read the same way.
    if "training_day_chips" in form:
        raw_days = ",".join(str(day) for day in form.getlist("training_day"))
    else:
        raw_days = truncate(str(form.get("training_days", "")), 100)
    days = normalize_days(raw_days)
    # A submission that parses to nothing is rejected rather than silently
    # stored as "no training days": that would quietly tell the planner the week
    # has no schedule at all, which is never what a typo meant. A blank field is
    # the one exception — that is an explicit "no fixed days", which the planner
    # renders as such.
    if raw_days.strip() and not days:
        return reject("No day recognised - use mon,tue,wed,thu,fri,sat,sun")

    # Same policy for the swim target, which previously had none: an
    # unparseable value silently became 0 and reported success, dropping
    # swimming from the plan on a typo.
    #
    # Blank is not a typo — the number input can legitimately be submitted empty
    # and 0 is the documented way to drop swimming from the plan, so blank and
    # absent both mean 0.
    #
    # Parsed with try/except rather than str.isdigit(): isdigit() is True for
    # '²' and for digit strings past CPython's int-conversion limit, both of
    # which raise in int() — an earlier version of this guard used isdigit() and
    # turned those inputs into a 500. truncate() bounds the length for the same
    # reason raw_days is bounded.
    raw_swim = truncate(str(form.get("training_swim_per_week", "")).strip(), 100)
    if raw_swim:
        try:
            swim = int(raw_swim)
        except ValueError:
            return reject(f"Swim sessions must be a whole number from 0 to {SWIM_PER_WEEK_MAX}")
        if not 0 <= swim <= SWIM_PER_WEEK_MAX:
            return reject(f"Swim sessions must be a whole number from 0 to {SWIM_PER_WEEK_MAX}")
    else:
        swim = 0

    await set_setting(db, SETTING_DAYS, ",".join(days))
    # `swim` is already bounded by the guard above. parse_swim_per_week keeps its
    # own clamp for the READ path (training_schedule.py), where it has to cope
    # with whatever is already stored — it is not redundant, just unreachable
    # from here.
    await set_setting(db, SETTING_SWIM, str(swim))

    return success_redirect(request, "/settings?tab=configuration", "Training schedule saved.")


# --- Automation settings ---


@router.post("/settings/automation")
async def save_automation(request: Request):
    from math import isfinite

    form = await request.form()
    db = get_user_db_from_request(request)

    # Validate every value before the first set_setting(), which commits independently.
    try:
        backup_interval = float(form.get("backup_interval_hours", "24"))
    except (TypeError, ValueError):
        return error_redirect(request, "/settings?tab=automation", "Backup interval must be a number from 1 to 168.")
    if not isfinite(backup_interval) or not 1 <= backup_interval <= 168:
        return error_redirect(request, "/settings?tab=automation", "Backup interval must be a number from 1 to 168.")

    try:
        backup_max = int(form.get("backup_max_copies", "7"))
    except (TypeError, ValueError):
        return error_redirect(
            request, "/settings?tab=automation", "Backup copies must be a whole number from 1 to 100."
        )
    if not 1 <= backup_max <= 100:
        return error_redirect(
            request, "/settings?tab=automation", "Backup copies must be a whole number from 1 to 100."
        )

    try:
        oura_interval = float(form.get("oura_sync_interval_hours", "6"))
    except (TypeError, ValueError):
        return error_redirect(request, "/settings?tab=automation", "Oura sync interval must be a number from 1 to 168.")
    if not isfinite(oura_interval) or not 1 <= oura_interval <= 168:
        return error_redirect(request, "/settings?tab=automation", "Oura sync interval must be a number from 1 to 168.")

    await set_setting(db, "backup_enabled", "1" if form.get("backup_enabled") else "0")
    await set_setting(db, "backup_interval_hours", str(backup_interval))
    await set_setting(db, "backup_max_copies", str(backup_max))
    await set_setting(db, "oura_sync_enabled", "1" if form.get("oura_sync_enabled") else "0")
    await set_setting(db, "oura_sync_interval_hours", str(oura_interval))
    await set_setting(db, "briefing_enabled", "1" if form.get("briefing_enabled") else "0")

    return success_redirect(request, "/settings?tab=automation", "Automation settings saved.")


# --- Feature Flags ---


async def _freedom_choices(db) -> list[dict]:
    """The Freedom checklist: the catalog, then the person's own habits."""
    from app.services.habits import PRESETS, PRESETS_BY_SLUG, list_habits

    stored = {h["slug"]: h for h in await list_habits(db, tracked_only=False)}
    choices = [
        {"slug": p["slug"], "name": p["name"], "tracked": bool(stored.get(p["slug"], {}).get("tracked"))}
        for p in PRESETS
    ]
    choices += [
        {"slug": h["slug"], "name": h["name"], "tracked": bool(h["tracked"])}
        for h in stored.values()
        if h["slug"] not in PRESETS_BY_SLUG
    ]
    return choices


@router.post("/settings/features")
async def save_features(request: Request):
    from app.services.habits import HabitError, add_custom, sync_tracked

    form = await request.form()
    db = get_user_db_from_request(request)
    # Union of flags already in the DB and any feature_* checkbox present in the form,
    # so a not-yet-seeded flag still saves instead of being silently dropped by an empty loop.
    known = set(await get_feature_flags(db))
    known |= {k.removeprefix("feature_") for k in form if k.startswith("feature_")}
    for flag_name in known:
        key = f"feature_{flag_name}"
        await set_setting(db, key, "1" if form.get(key) else "0")

    # The habit checklist only applies while Freedom is on; switching the module
    # off keeps the selection for when it comes back.
    if form.get("feature_freedom") and form.get("freedom_habits_shown"):
        # Only from a form that showed the box: an unchecked box sends nothing,
        # so without the marker any older form would switch the block off.
        if form.get("freedom_dashboard_shown"):
            await set_setting(db, "freedom_on_dashboard", "1" if form.get("freedom_on_dashboard") else "0")
        await sync_tracked(db, set(form.getlist("habits")))
        custom = (form.get("custom_habit") or "").strip()
        if custom:
            try:
                await add_custom(db, custom)
            except HabitError as exc:
                return error_redirect(request, "/settings?tab=general", str(exc))

    return success_redirect(request, "/settings?tab=general", "Feature settings updated.")


# --- Backup ---


@router.post("/settings/backup/now")
async def trigger_backup_now(request: Request, job_nonce: str = Form(...)):
    from app.services.job_producers import BACKUP_JOB_KIND, enqueue_backup_job, manual_job_key

    db = get_user_db_from_request(request)
    try:
        result = await enqueue_backup_job(
            db,
            trigger="manual",
            idempotency_key=manual_job_key(BACKUP_JOB_KIND, job_nonce),
        )
    except Exception:
        logger.exception("Manual backup enqueue failed")
        return error_redirect(request, "/settings?tab=automation", "Backup could not be queued. Try again.")
    from app.routers.jobs import wake_worker_for

    wake_worker_for(request)

    return success_redirect(
        request,
        f"/settings?tab=automation&job_id={result.job_id}",
        "Backup queued.",
    )


# --- Export ---


@router.post("/settings/export")
async def export_markdown_download(request: Request):
    """Download a readable markdown summary of the chosen period and sections, for
    reading or pasting into an LLM. Built on request; nothing is written to disk."""
    from fastapi.responses import Response

    from app.services.markdown_export import EXPORT_SCOPES, EXPORT_SECTIONS, export_markdown

    form = await request.form()
    scope = form.get("scope", "weekly")
    if scope not in EXPORT_SCOPES:
        return error_redirect(request, "/settings?tab=data", "Choose a valid export scope.")
    sections = form.getlist("sections")
    if any(section not in EXPORT_SECTIONS for section in sections):
        return error_redirect(request, "/settings?tab=data", "Choose only valid export sections.")
    if not sections:
        return error_redirect(request, "/settings?tab=data", "Choose at least one export section.")
    content = await export_markdown(get_user_db_from_request(request), scope, sections=set(sections))
    filename = f"virgil-{scope}-{date.today().isoformat()}.md"
    return Response(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@router.get("/settings/export/json")
async def export_json(request: Request):
    from app.services.data_transfer import export_document

    document = await export_document(get_user_db_from_request(request))
    content = await asyncio.to_thread(json.dumps, document, indent=1, ensure_ascii=False)
    filename = f"virgil-export-{document['exported_at'][:10]}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}", "Cache-Control": "no-store"},
    )


def _db_stem(request: Request) -> str:
    return request.state.user["db_filename"].removesuffix(".db")


@router.post("/settings/import")
async def import_upload(request: Request):
    """Check an uploaded JSON export without changing anything, then show what it would restore."""
    from starlette.datastructures import UploadFile

    from app.services.data_transfer import IMPORT_MAX_BYTES, ImportRefused, new_upload, stage_upload

    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return error_redirect(request, "/settings?tab=data", "Choose a Virgil JSON export to restore.")
    stem = _db_stem(request)
    token, path = new_upload(stem)
    size = 0
    try:
        with path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > IMPORT_MAX_BYTES:
                    raise ImportRefused(f"The file is larger than {IMPORT_MAX_BYTES // (1024 * 1024)} MB.")
                await asyncio.to_thread(handle.write, chunk)
        await stage_upload(stem, token)
    except ImportRefused as refused:
        path.unlink(missing_ok=True)
        return error_redirect(request, "/settings?tab=data", str(refused))
    except Exception:
        path.unlink(missing_ok=True)
        logger.exception("Import check failed")
        return error_redirect(request, "/settings?tab=data", "The file could not be checked. Nothing was changed.")
    return success_redirect(request, f"/settings/import/{token}", "File checked. Nothing has changed yet.")


@router.get("/settings/import/{token}", response_class=HTMLResponse)
async def import_review(request: Request, token: str):
    from app.migrations.runner import latest_version
    from app.services.data_transfer import DATA_TABLES, ImportRefused, current_counts, staged_report, table_label

    try:
        report = staged_report(_db_stem(request), token)
    except ImportRefused:
        report = None
    if report is None:
        return error_redirect(request, "/settings?tab=data", "That import has expired. Upload the file again.")
    current = await current_counts(get_user_db_from_request(request))
    rows = [
        {"label": table_label(table), "current": current.get(table, 0), "incoming": report["counts"].get(table, 0)}
        for table in DATA_TABLES
        if current.get(table) or report["counts"].get(table)
    ]
    return templates.TemplateResponse(
        request,
        "settings_import.html",
        {"token": token, "report": report, "rows": rows, "latest_schema": latest_version()},
        headers={"Cache-Control": "no-store"},
    )


@router.post("/settings/import/{token}/apply")
async def import_apply(request: Request, token: str):
    from app.central_db import user_lifecycle_operation
    from app.services.backup import snapshot_before_import
    from app.services.data_transfer import ImportRefused, apply_staged, staged_report

    stem = _db_stem(request)
    try:
        if staged_report(stem, token) is None:
            raise ImportRefused("That import has expired. Upload the file again.")
    except ImportRefused as refused:
        return error_redirect(request, "/settings?tab=data", str(refused))
    db = get_user_db_from_request(request)
    async with user_lifecycle_operation(request.state.user["id"], "data-import") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(request, "/settings?tab=data", "Another account operation is already in progress.")
        try:
            await snapshot_before_import(db)
        except Exception:
            logger.exception("Pre-import snapshot failed")
            return error_redirect(
                request, "/settings?tab=data", "No copy of your current data could be saved, so nothing was imported."
            )
        try:
            await apply_staged(db, stem, token)
        except ImportRefused as refused:
            return error_redirect(request, "/settings?tab=data", str(refused))
    logger.info("JSON import applied for user %s", request.state.user["id"])
    return success_redirect(request, "/settings?tab=data", "Data restored from the export.")


@router.post("/settings/import/{token}/cancel")
async def import_cancel(request: Request, token: str):
    from app.services.data_transfer import ImportRefused, discard

    with contextlib.suppress(ImportRefused):
        discard(_db_stem(request), token)
    return success_redirect(request, "/settings?tab=data", "Import cancelled. Nothing was changed.")


@router.get("/settings/export/csv")
async def export_csv(request: Request):
    from app.services.data_transfer import DATA_TABLES

    db = get_user_db_from_request(request)
    output = io.StringIO()
    for table in DATA_TABLES:
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
    if not provider or not model or not api_key.strip():
        return error_redirect(request, "/settings?tab=general", "Provider, model, and API key are required.")
    await db.execute(
        "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 0)",
        (provider, encrypt(api_key), model),
    )
    await db.commit()
    return success_redirect(request, "/settings?tab=general", "LLM provider added.")


@router.post("/settings/llm/activate")
async def activate_llm_provider(request: Request, provider_id: int = Form(...)):
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT id FROM llm_providers WHERE id = ?", (provider_id,))
    if not rows:
        return error_redirect(request, "/settings?tab=general", "LLM provider not found.")
    await db.execute("UPDATE llm_providers SET is_active = 0")
    cursor = await db.execute("UPDATE llm_providers SET is_active = 1 WHERE id = ?", (provider_id,))
    if cursor.rowcount != 1:
        await db.rollback()
        return error_redirect(request, "/settings?tab=general", "LLM provider was not activated.")
    await db.commit()
    return success_redirect(request, "/settings?tab=general", "LLM provider activated.")


@router.post("/settings/llm/reasoning")
async def save_llm_reasoning(request: Request, reasoning_effort: str = Form(...)):
    if reasoning_effort not in REASONING_EFFORTS:
        return error_redirect(request, "/settings?tab=general", "Choose one of the listed thinking levels.")
    db = get_user_db_from_request(request)
    await set_setting(db, REASONING_EFFORT_SETTING, reasoning_effort)
    return success_redirect(request, "/settings?tab=general", "Thinking level saved.")


@router.post("/settings/llm/delete")
async def delete_llm_provider(request: Request, provider_id: int = Form(...)):
    db = get_user_db_from_request(request)
    cursor = await db.execute("DELETE FROM llm_providers WHERE id = ?", (provider_id,))
    if cursor.rowcount != 1:
        await db.rollback()
        return error_redirect(request, "/settings?tab=general", "LLM provider not found.")
    await db.commit()
    return success_redirect(request, "/settings?tab=general", "LLM provider deleted.")


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

    from app.central_db import heartbeat_user_lifecycle, update_user, user_lifecycle_operation
    from app.services.backup import delete_user_backups
    from app.user_db import create_user_db, delete_user_db

    user = getattr(request.state, "user", None)
    if not user or not user.get("db_filename"):
        return error_redirect(request, "/login", "Sign in again before resetting data.")

    db = get_user_db_from_request(request)
    async with user_lifecycle_operation(user["id"], "factory-reset") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(
                request, "/settings?tab=security", "Another account operation is already in progress."
            )
        if not await _disable_subscription_now(db, user["id"], "factory-reset"):
            return error_redirect(
                request,
                "/settings?tab=security",
                "Factory reset stopped because remote subscriptions could not be removed. Try again later.",
            )
        if not await heartbeat_user_lifecycle(user["id"], lifecycle_claim):
            raise RuntimeError("Factory reset lifecycle lease was lost before database replacement")

        old_filename = user["db_filename"]
        new_filename = f"{uuid.uuid4()}.db"
        await create_user_db(new_filename)
        await update_user(user["id"], db_filename=new_filename)
        delete_user_db(old_filename)
        delete_user_backups(old_filename)

        logger.info("Factory reset completed for user %s", user["email"])
        return success_redirect(request, "/onboarding", "Factory reset completed. Start onboarding again.")


# --- Oura Integration ---


@router.post("/settings/oura/save")
async def save_oura_credentials(
    request: Request,
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    client_id = client_id.strip()
    client_secret = client_secret.strip()
    if not client_id or not client_secret:
        return error_redirect(request, "/settings?tab=integrations", "Oura client ID and client secret are required.")

    from app.central_db import get_subscription_registration, user_lifecycle_operation

    user_id = request.state.user["id"]
    async with user_lifecycle_operation(user_id, "oura-credentials") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(
                request, "/settings?tab=integrations", "Another account operation is already in progress."
            )
        if await get_subscription_registration(user_id, "oura"):
            return error_redirect(
                request,
                "/settings?tab=integrations",
                "Disable the Oura webhook before changing application credentials.",
            )

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
        return success_redirect(request, "/settings?tab=integrations", "Oura credentials saved.")


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
    from app.central_db import heartbeat_user_lifecycle, user_lifecycle_operation

    user_id = request.state.user["id"]
    async with user_lifecycle_operation(user_id, "oura-disconnect") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(
                request, "/settings?tab=integrations", "Another account operation is already in progress."
            )
        db = get_user_db_from_request(request)
        rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
        if not rows:
            return error_redirect(request, "/settings?tab=integrations", "Oura integration is not configured.")
        if rows[0]["status"] != "connected":
            return error_redirect(request, "/settings?tab=integrations", "Oura is not connected.")
        if not await _disable_subscription_now(db, user_id, "disconnect"):
            return error_redirect(
                request,
                "/settings?tab=integrations",
                "Oura was not disconnected because remote subscriptions could not be removed.",
            )
        if not await heartbeat_user_lifecycle(user_id, lifecycle_claim):
            raise RuntimeError("Oura disconnect lifecycle lease was lost")

        cursor = await db.execute(
            """UPDATE integrations SET access_token_enc = '', refresh_token_enc = '',
               token_expires_at = '', status = 'configured' WHERE provider = 'oura'"""
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return error_redirect(request, "/settings?tab=integrations", "Oura was not disconnected.")
        await db.commit()
        return success_redirect(request, "/settings?tab=integrations", "Oura disconnected.")


@router.post("/settings/oura/sync")
async def oura_sync(request: Request, job_nonce: str = Form(...)):
    from app.services.job_producers import OURA_SYNC_JOB_KIND, enqueue_oura_sync_job, manual_job_key

    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
    if not rows or rows[0]["status"] != "connected":
        return error_redirect(request, "/settings?tab=integrations", "Oura is not connected.")
    try:
        result = await enqueue_oura_sync_job(
            db,
            trigger="manual",
            days_back=30,
            idempotency_key=manual_job_key(OURA_SYNC_JOB_KIND, job_nonce),
        )
    except Exception:
        logger.exception("Oura sync enqueue failed")
        return error_redirect(request, "/settings?tab=integrations", "Oura sync could not be queued.")
    from app.routers.jobs import wake_worker_for

    wake_worker_for(request)

    return success_redirect(
        request,
        f"/settings?tab=integrations&job_id={result.job_id}",
        "Oura sync queued.",
    )


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


async def _disable_subscription_now(db, user_id: str, trigger: str) -> bool:
    from app.central_db import get_subscription_registration
    from app.services.subscriptions import disable_subscription, reconcile_subscription

    if not await get_subscription_registration(user_id, "oura"):
        return True
    await disable_subscription(user_id, "oura")
    try:
        result = await reconcile_subscription(
            user_id,
            "oura",
            db,
            worker_id=f"settings:{trigger}:{user_id}",
        )
    except Exception:
        logger.exception("Immediate Oura subscription disable failed")
        return False
    return result is not None and result.status == "disabled"


@router.post("/settings/oura/webhook/enable")
async def enable_oura_webhook(request: Request):
    from app.central_db import create_webhook_route, get_subscription_registration, user_lifecycle_operation
    from app.services.subscriptions import enable_subscription, reconcile_subscription

    db = get_user_db_from_request(request)
    user = request.state.user
    async with user_lifecycle_operation(user["id"], "oura-webhook-enable") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(
                request, "/settings?tab=integrations", "Another account operation is already in progress."
            )
        # Events can only be synced with a live token, so require a connected
        # integration even though subscription management uses app credentials.
        token = await ensure_valid_token(db)
        creds = await _oura_client_credentials(db)
        if not token or not creds:
            return error_redirect(
                request, "/settings?tab=integrations", "Connect Oura again before enabling the webhook."
            )

        if await get_subscription_registration(user["id"], "oura"):
            return error_redirect(request, "/settings?tab=integrations", "Oura webhook is already enabled.")
        client_id, _client_secret = creds
        verification_token = secrets.token_urlsafe(32)
        webhook_id = await create_webhook_route(user["id"], "oura", lifecycle_claim=lifecycle_claim)
        callback_url = f"{BASE_URL.rstrip('/')}/api/oura/webhook/{webhook_id}"
        await enable_subscription(
            user["id"],
            "oura",
            callback_url,
            client_id,
            lifecycle_claim=lifecycle_claim,
        )
        await db.execute(
            "UPDATE integrations SET webhook_secret = ? WHERE provider = 'oura'",
            (encrypt(verification_token),),
        )
        await set_setting(db, "oura_webhook_id", webhook_id)

        try:
            result = await reconcile_subscription(
                user["id"],
                "oura",
                db,
                worker_id=f"settings:enable:{user['id']}",
            )
        except Exception:
            logger.exception("Immediate Oura subscription enable failed")
            return error_redirect(
                request,
                "/settings?tab=integrations",
                "Webhook setup is pending and will retry automatically.",
            )
        if result is None:
            return success_redirect(request, "/settings?tab=integrations", "Webhook setup queued.")
        if result.status == "active":
            return success_redirect(request, "/settings?tab=integrations", "Oura webhook enabled.")
        return error_redirect(
            request, "/settings?tab=integrations", "Webhook is partially active and will retry automatically."
        )


@router.post("/settings/oura/webhook/disable")
async def disable_oura_webhook(request: Request):
    db = get_user_db_from_request(request)
    user = request.state.user
    from app.central_db import get_subscription_registration, user_lifecycle_operation

    async with user_lifecycle_operation(user["id"], "oura-webhook-disable") as lifecycle_claim:
        if lifecycle_claim is None:
            return error_redirect(
                request, "/settings?tab=integrations", "Another account operation is already in progress."
            )
        if not await get_subscription_registration(user["id"], "oura"):
            return error_redirect(request, "/settings?tab=integrations", "Oura webhook is not enabled.")
        if await _disable_subscription_now(db, user["id"], "manual"):
            return success_redirect(request, "/settings?tab=integrations", "Oura webhook disabled.")
        return error_redirect(
            request,
            "/settings?tab=integrations",
            "Webhook disable is pending and will retry automatically.",
        )
