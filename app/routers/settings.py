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
from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.encryption import decrypt, encrypt
from app.services.llm import REASONING_EFFORT_SETTING, REASONING_EFFORTS, resolve_reasoning_effort
from app.services.oura_api import (
    create_webhook_subscription,
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

    if job_id is not None and tab != "automation":
        from app.routers.jobs import build_job_view
        from app.services.jobs import get_job_status

        job = await get_job_status(db, job_id)
        context["current_job"] = build_job_view(job) if job is not None else None

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
        context["reasoning_efforts"] = list(REASONING_EFFORTS)
        context["reasoning_effort"] = await resolve_reasoning_effort(db)

    elif tab == "configuration":
        from app.library_validation import LIBRARY_SECTIONS
        from app.services.training_schedule import DEFAULT_DAYS, DEFAULT_SWIM, SETTING_DAYS, SETTING_SWIM

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
        context["training_swim_per_week"] = await get_setting(db, SETTING_SWIM, DEFAULT_SWIM)
        context["library_all_tags"] = sorted({tr["tag"] for tr in tag_rows})

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
        from app.routers.jobs import build_job_view
        from app.services.jobs import list_recent_job_statuses

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
        context["recent_jobs"] = [build_job_view(row) for row in await list_recent_job_statuses(db)]

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
    section: str = Form("Core"),
    sets: str = Form(""),
    reps: str = Form(""),
    notes: str = Form(""),
    metric: str = Form("reps"),
    tags: str = Form(""),
):
    from app.library_validation import LibraryWriteError, normalize_tags, validate_library_write

    try:
        sets_val = int(sets) if sets.strip() else None
    except ValueError:
        return error_redirect(request, "/settings?tab=configuration", "Sets must be a whole number.")

    db = get_user_db_from_request(request)
    try:
        row = await validate_library_write(
            db,
            op="create",
            fields={"name": name, "section": section, "sets": sets_val, "reps": reps, "notes": notes, "metric": metric},
        )
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)

    # normalize_tags accepts the raw comma-separated field straight from the
    # form. Run it before the INSERT so a bad tag (e.g. one that normalises
    # to nothing) 422s-equivalent-redirects without creating an orphan row.
    try:
        tag_list = normalize_tags(tags)
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)

    cursor = await db.execute(
        "INSERT INTO exercise_library "
        "(section, name, sets, reps, notes, display_order, metric, builtin) "
        "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(display_order), 0) + 1 FROM exercise_library), ?, 0)",
        (row["section"], row["name"], row["sets"], row["reps"], row["notes"], row["metric"]),
    )
    for tag in tag_list:
        await db.execute("INSERT INTO exercise_library_tags (library_id, tag) VALUES (?, ?)", (cursor.lastrowid, tag))
    await db.commit()
    return success_redirect(request, "/settings?tab=configuration", "Exercise added to the library.")


@router.post("/settings/library/update")
async def library_update(
    request: Request,
    entry_id: int = Form(...),
    # None (absent from the POST body) means "leave this column unchanged" —
    # NOT "reset to a default". A stale settings page cached from before this
    # branch added the metric <select> would omit `metric` entirely on
    # submit; with a string default here that silently downgraded a 'time'
    # movement to 'reps' on every such save (I1). Form(None) lets FastAPI
    # tell "field absent" (None) apart from "field present but blank" ('') —
    # the latter still means "clear it", same as api.py's PATCH.
    # `name` joined this group so a builtin row's tag-only edit form (which
    # never renders name/section/etc — those stay frozen for builtin rows)
    # can omit it entirely without tripping the "name is required"
    # validation or the builtin guard in validate_library_write.
    name: str | None = Form(None),
    section: str | None = Form(None),
    sets: str | None = Form(None),
    reps: str | None = Form(None),
    notes: str | None = Form(None),
    metric: str | None = Form(None),
    # Tags live in exercise_library_tags, outside validate_library_write's
    # scope entirely — never gated by builtin ON THEIR OWN (unlike every
    # field above). But `fields` above still reaches validate_library_write
    # unconditionally, so a POST that mixes `tags` with one of those frozen
    # fields on a builtin row is refused in full below (the tags never land
    # either) — tag a builtin row in its own request, never together with a
    # name/section/sets/reps/notes/metric edit. Same contract as the REST
    # PATCH endpoint: None (absent) leaves tags untouched, "" (present but
    # blank) clears them, anything else replaces the whole set.
    tags: str | None = Form(None),
):
    from app.library_validation import LibraryWriteError, normalize_tags, validate_library_write

    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
    if not rows:
        return error_redirect(request, "/settings?tab=configuration", "Library entry not found.")
    existing = dict(rows[0])

    fields: dict = {}
    if name is not None:
        fields["name"] = name
    if section is not None:
        fields["section"] = section
    if sets is not None:
        try:
            # sets / reps stopped being a prescription when the protocol form was
            # deleted (2026-08-01). They survive as the WOD parser's fallback
            # values and as columns in this listing. Nothing reads them as
            # "do 4 sets of 10" any more, so do not reintroduce that meaning.
            fields["sets"] = int(sets) if sets.strip() else None
        except ValueError:
            return error_redirect(request, "/settings?tab=configuration", "Sets must be a whole number.")
    if reps is not None:
        fields["reps"] = reps
    if notes is not None:
        fields["notes"] = notes
    if metric is not None:
        fields["metric"] = metric

    try:
        result = await validate_library_write(db, op="update", entry_id=entry_id, existing=existing, fields=fields)
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)

    # M2 (2026-07-31 review): normalize_tags must run — and be allowed to
    # raise — BEFORE the UPDATE below, not after. This used to run after,
    # with no db.rollback() on the except branch below, so a bad tag left the
    # UPDATE's effect sitting uncommitted on this request's connection. It
    # only ever looked atomic because auth.py opens a brand new connection
    # per request and closes it in a `finally` without ever committing on an
    # exception path — introduce any form of connection reuse/pooling and
    # this starts committing half of a rejected write. Validating first (same
    # order the add path above already uses) makes that true regardless of
    # what closes the connection, instead of by accident.
    tag_list = None
    if tags is not None:
        try:
            tag_list = normalize_tags(tags)
        except LibraryWriteError as exc:
            return error_redirect(request, "/settings?tab=configuration", exc.message)

    if not result and tags is None:
        return error_redirect(request, "/settings?tab=configuration", "No library changes were submitted.")

    if result:
        assignments = ", ".join(f"{k} = ?" for k in result)
        cursor = await db.execute(
            f"UPDATE exercise_library SET {assignments} WHERE id = ?",  # noqa: S608 — keys are this
            # module's own known column names (validate_library_write's fixed key set), never
            # attacker-controlled.
            [*result.values(), entry_id],
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return error_redirect(request, "/settings?tab=configuration", "Library entry was not updated.")

    # Tags themselves bypass the builtin guard — never routed through
    # `fields`/validate_library_write above, so a builtin row accepts a
    # tags-ONLY update even though name/section/metric/sets/reps/notes on it
    # stay frozen. That guard is still all-or-nothing per request, though:
    # if `fields` was non-empty and the row builtin, validate_library_write
    # above already raised and returned before this line — so a request
    # combining `tags` with one of those frozen fields never reaches here,
    # and the tags don't land either. Same replace-the-whole-set semantics,
    # and the same combined-request rejection, as api.py's PATCH.
    if tags is not None:
        await db.execute("DELETE FROM exercise_library_tags WHERE library_id = ?", (entry_id,))
        for tag in tag_list:
            await db.execute("INSERT INTO exercise_library_tags (library_id, tag) VALUES (?, ?)", (entry_id, tag))

    if result or tags is not None:
        await db.commit()
    return success_redirect(request, "/settings?tab=configuration", "Library entry updated.")


@router.post("/settings/library/delete")
async def library_delete(request: Request, entry_id: int = Form(...)):
    from app.library_validation import LibraryWriteError, validate_library_write

    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
    if not rows:
        return error_redirect(request, "/settings?tab=configuration", "Library entry not found.")
    try:
        await validate_library_write(db, op="delete", entry_id=entry_id, existing=dict(rows[0]))
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)

    cursor = await db.execute("DELETE FROM exercise_library WHERE id = ?", (entry_id,))
    if cursor.rowcount != 1:
        await db.rollback()
        return error_redirect(request, "/settings?tab=configuration", "Library entry was not deleted.")
    await db.commit()
    return success_redirect(request, "/settings?tab=configuration", "Exercise deleted from the library.")


@router.post("/settings/library/archive")
async def library_archive(request: Request, entry_id: int = Form(...), archived: int = Form(1)):
    # M1 (2026-07-31 review): this was the one library write with no existence
    # check and no validate_library_write call at all -- a bogus id silently
    # updated zero rows and redirected as if it had succeeded, while api.py's
    # PATCH 404s for the same input. That's the exact "returns success having
    # done nothing" shape library_validation.py's module docstring says
    # validate_library_write exists to rule out for both surfaces together.
    # Archiving a builtin row is still allowed here (unchanged behaviour):
    # validate_library_write's builtin guard only fires when `fields` carries
    # something OTHER than `archived` (see its op="update" branch), so
    # fields={"archived": ...} alone never trips it.
    from app.library_validation import LibraryWriteError, validate_library_write

    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
    if not rows:
        return error_redirect(request, "/settings?tab=configuration", "Library entry not found.")
    existing = dict(rows[0])

    target_archived = 1 if archived else 0
    if existing["archived"] == target_archived:
        state = "archived" if target_archived else "active"
        return error_redirect(request, "/settings?tab=configuration", f"Library entry is already {state}.")

    try:
        result = await validate_library_write(
            db, op="update", entry_id=entry_id, existing=existing, fields={"archived": target_archived}
        )
    except LibraryWriteError as exc:
        return error_redirect(request, "/settings?tab=configuration", exc.message)

    if result:
        assignments = ", ".join(f"{k} = ?" for k in result)
        cursor = await db.execute(
            f"UPDATE exercise_library SET {assignments} WHERE id = ?",  # noqa: S608 — see library_update
            [*result.values(), entry_id],
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return error_redirect(request, "/settings?tab=configuration", "Library entry was not updated.")
        await db.commit()
    action = "archived" if target_archived else "restored"
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
    raw_days = truncate(str(form.get("training_days", "")), 100)
    days = normalize_days(raw_days)
    # A submission that parses to nothing is rejected rather than silently
    # stored as "no training days": that would quietly tell the planner the week
    # has no schedule at all, which is never what a typo meant. A blank field is
    # the one exception — that is an explicit "no fixed days", which the planner
    # renders as such.
    if raw_days.strip() and not days:
        return reject("Nie rozpoznano żadnego dnia — użyj mon,tue,wed,thu,fri,sat,sun")

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
            return reject(f"Liczba basenów musi być liczbą całkowitą 0–{SWIM_PER_WEEK_MAX}")
        if not 0 <= swim <= SWIM_PER_WEEK_MAX:
            return reject(f"Liczba basenów musi być liczbą całkowitą 0–{SWIM_PER_WEEK_MAX}")
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

    try:
        export_interval = float(form.get("export_interval_hours", "6"))
    except (TypeError, ValueError):
        return error_redirect(request, "/settings?tab=automation", "Export interval must be a number from 1 to 168.")
    if not isfinite(export_interval) or not 1 <= export_interval <= 168:
        return error_redirect(request, "/settings?tab=automation", "Export interval must be a number from 1 to 168.")

    await set_setting(db, "backup_enabled", "1" if form.get("backup_enabled") else "0")
    await set_setting(db, "backup_interval_hours", str(backup_interval))
    await set_setting(db, "backup_max_copies", str(backup_max))
    await set_setting(db, "oura_sync_enabled", "1" if form.get("oura_sync_enabled") else "0")
    await set_setting(db, "oura_sync_interval_hours", str(oura_interval))
    await set_setting(db, "briefing_enabled", "1" if form.get("briefing_enabled") else "0")
    await set_setting(db, "export_enabled", "1" if form.get("export_enabled") else "0")
    await set_setting(db, "export_interval_hours", str(export_interval))

    return success_redirect(request, "/settings?tab=automation", "Automation settings saved.")


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
    return success_redirect(
        request,
        f"/settings?tab=automation&job_id={result.job_id}",
        "Backup queued.",
    )


# --- Export ---


@router.post("/settings/export")
async def trigger_export(request: Request, job_nonce: str = Form(...)):
    from app.services.job_producers import (
        EXPORT_SCOPES,
        EXPORT_SECTIONS,
        MARKDOWN_EXPORT_JOB_KIND,
        enqueue_markdown_export_job,
        manual_job_key,
    )

    form = await request.form()
    scope = form.get("scope", "weekly")
    if scope not in EXPORT_SCOPES:
        return error_redirect(request, "/settings?tab=data", "Choose a valid export scope.")
    sections = form.getlist("sections")
    if any(section not in EXPORT_SECTIONS for section in sections):
        return error_redirect(request, "/settings?tab=data", "Choose only valid export sections.")
    if not sections:
        return error_redirect(request, "/settings?tab=data", "Choose at least one export section.")
    db = get_user_db_from_request(request)
    try:
        result = await enqueue_markdown_export_job(
            db,
            trigger="manual",
            scope=scope,
            sections=sections,
            idempotency_key=manual_job_key(MARKDOWN_EXPORT_JOB_KIND, job_nonce),
        )
    except Exception:
        logger.exception("Export enqueue failed")
        return error_redirect(request, "/settings?tab=data", "Export could not be queued. Try again.")
    return success_redirect(
        request,
        f"/settings?tab=data&job_id={result.job_id}",
        f"{scope.capitalize()} export queued.",
    )


@router.post("/settings/import")
async def trigger_import(request: Request):
    from app.services.markdown_import import import_all

    db = get_user_db_from_request(request)
    try:
        await import_all(db)
        return success_redirect(request, "/settings?tab=data", "Import completed.")
    except Exception:
        logger.exception("Import failed")
        return error_redirect(request, "/settings?tab=data", "Import failed. Try again.")


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
    "exercise_library_tags",
    "feniks_config",
    "feniks_journal",
    "feniks_pleasures",
    "feniks_milestones",
    "feniks_daily",
    "feniks_bricks",
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

    from app.central_db import delete_webhook_routes, update_user
    from app.user_db import create_user_db, delete_user_db

    user = getattr(request.state, "user", None)
    if not user or not user.get("db_filename"):
        return error_redirect(request, "/login", "Sign in again before resetting data.")

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
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
    if not rows:
        return error_redirect(request, "/settings?tab=integrations", "Oura integration is not configured.")
    if rows[0]["status"] != "connected":
        return error_redirect(request, "/settings?tab=integrations", "Oura is not connected.")

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
        return error_redirect(request, "/settings?tab=integrations", "Connect Oura again before enabling the webhook.")

    existing_webhook_id = await get_setting(db, "oura_webhook_id", "")
    existing_webhook = await db.execute_fetchall("SELECT webhook_secret FROM integrations WHERE provider = 'oura'")
    if existing_webhook_id and existing_webhook and existing_webhook[0]["webhook_secret"]:
        return error_redirect(request, "/settings?tab=integrations", "Oura webhook is already enabled.")
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
                return error_redirect(
                    request,
                    "/settings?tab=integrations",
                    f"Webhook partially enabled; no events for: {failed_types}. Disable it and retry.",
                )
            return success_redirect(request, "/settings?tab=integrations", "Oura webhook enabled.")
        except Exception:
            logger.exception("Failed to create Oura webhook subscription")
            # Roll back local state since no subscription exists
            await db.execute("UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'")
            await set_setting(db, "oura_webhook_id", "")
            await delete_webhook_routes(user["id"])
            return error_redirect(request, "/settings?tab=integrations", "Oura webhook registration failed. Try again.")


@router.post("/settings/oura/webhook/disable")
async def disable_oura_webhook(request: Request):
    from app.central_db import delete_webhook_routes

    db = get_user_db_from_request(request)
    user = request.state.user

    rows = await db.execute_fetchall("SELECT webhook_secret FROM integrations WHERE provider = 'oura'")
    webhook_id = await get_setting(db, "oura_webhook_id", "")
    if not webhook_id and (not rows or not rows[0]["webhook_secret"]):
        return error_redirect(request, "/settings?tab=integrations", "Oura webhook is not enabled.")

    async with _oura_webhook_lock:
        await _reconcile_oura_subscriptions(db)

        await db.execute("UPDATE integrations SET webhook_secret = '' WHERE provider = 'oura'")
        await set_setting(db, "oura_webhook_id", "")
        await delete_webhook_routes(user["id"])
    return success_redirect(request, "/settings?tab=integrations", "Oura webhook disabled.")
