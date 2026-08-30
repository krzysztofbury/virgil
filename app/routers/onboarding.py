import logging
import math
import secrets

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse

from app.db import set_setting
from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.models.user_profile import (
    ensure_profile,
    get_profile,
    update_step1,
    update_step2,
    update_step3,
    update_step4,
)
from app.user_db import get_user_db_from_request
from app.validation import truncate

logger = logging.getLogger(__name__)

router = APIRouter()

ENRICHMENT_JOB_KIND = "onboarding_enrichment"
MEDICAL_JOB_KIND = "medical_import"
MEDICAL_TEXT_MAX_CHARS = 10000
ALLOWED_EQUIPMENT = {
    "home_gym",
    "gym_membership",
    "resistance_bands",
    "kettlebells",
    "pullup_bar",
    "bodyweight",
}


def _safe_float(value: str) -> float | None:
    """Parse a form string to float, returning None on invalid input."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = float(stripped)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


GOAL_CATEGORY_MAP = {
    "mind": ["Duchowość", "Rozwój"],
    "body": ["Zdrowie"],
    "finance": ["Planowanie Życia", "Praca"],
    "relations": ["Rodzina", "Życie Towarzyskie", "Relaks"],
}


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, step: int = 0, job_id: int | None = Query(None, ge=1)):
    step = max(0, min(6, step))
    db = get_user_db_from_request(request)
    profile = await ensure_profile(db)

    # If no step specified, resume from where user left off.
    if step == 0:
        step = min(profile["onboarding_step"] + 1, 6)

    # Load goals for step 3 display and step 6 summary.
    goals = {}
    if step in (3, 6):
        rows = await db.execute_fetchall(
            """SELECT ga.name as area_name, g.content
               FROM goals g JOIN goal_areas ga ON g.area_id = ga.id
               WHERE g.horizon = '10yr' ORDER BY ga.display_order"""
        )
        for row in rows:
            goals[row["area_name"]] = row["content"]

    # Check if internal LLM is available (for step 5 info).
    from app.config import INTERNAL_LLM_KEY
    from app.services.onboarding import enrichment_progress

    has_internal_llm = bool(INTERNAL_LLM_KEY)

    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "step": step,
            "profile": profile,
            "goals": goals,
            "has_internal_llm": has_internal_llm,
            "current_job": await _job_view(db, job_id),
            "enrichment": await enrichment_progress(db),
        },
    )


async def _job_view(db, job_id: int | None) -> dict | None:
    if job_id is None:
        return None
    from app.routers.jobs import build_job_view
    from app.services.jobs import get_job_status

    job = await get_job_status(db, job_id)
    return build_job_view(job) if job is not None else None


@router.post("/onboarding/step/1")
async def save_step1(
    request: Request,
    sex: str = Form(""),
    age: str = Form(""),
    height_cm: str = Form(""),
    weight_kg: str = Form(""),
    family: str = Form(""),
    habits_good: str = Form(""),
    habits_bad: str = Form(""),
):
    if sex not in {"", "male", "female"}:
        return error_redirect(request, "/onboarding?step=1", "Choose a valid sex option.")

    age_value = None
    if age.strip():
        try:
            age_value = int(age)
        except ValueError:
            return error_redirect(request, "/onboarding?step=1", "Enter age as a whole number from 10 to 120.")
        if not 10 <= age_value <= 120:
            return error_redirect(request, "/onboarding?step=1", "Enter an age from 10 to 120.")

    height_value = _safe_float(height_cm)
    if height_cm.strip() and (height_value is None or height_value <= 0):
        return error_redirect(request, "/onboarding?step=1", "Enter height as a positive number.")

    weight_value = _safe_float(weight_kg)
    if weight_kg.strip() and (weight_value is None or weight_value <= 0):
        return error_redirect(request, "/onboarding?step=1", "Enter weight as a positive number.")

    db = get_user_db_from_request(request)
    await update_step1(
        db,
        sex=truncate(sex, 20),
        age=age_value,
        height_cm=height_value,
        weight_kg=weight_value,
        family=truncate(family, 500),
        habits_good=truncate(habits_good, 2000),
        habits_bad=truncate(habits_bad, 2000),
    )
    return success_redirect(request, "/onboarding?step=2", "About-you details saved.")


@router.post("/onboarding/step/2")
async def save_step2(request: Request, ideal_day: str = Form("")):
    db = get_user_db_from_request(request)
    await update_step2(db, ideal_day=truncate(ideal_day, 5000))
    return success_redirect(request, "/onboarding?step=3", "Ideal-day details saved.")


@router.post("/onboarding/step/3")
async def save_step3(
    request: Request,
    goal_mind: str = Form(""),
    goal_body: str = Form(""),
    goal_finance: str = Form(""),
    goal_relations: str = Form(""),
):
    db = get_user_db_from_request(request)

    # Save end goals (Level 3 / 10yr) to goals table.
    category_goals = {
        "mind": truncate(goal_mind, 1000),
        "body": truncate(goal_body, 1000),
        "finance": truncate(goal_finance, 1000),
        "relations": truncate(goal_relations, 1000),
    }

    for category, content in category_goals.items():
        if not content.strip():
            continue
        # Map to the first goal area in the category.
        area_name = GOAL_CATEGORY_MAP[category][0]
        area_row = await db.execute_fetchall("SELECT id FROM goal_areas WHERE name = ?", (area_name,))
        if area_row:
            area_id = area_row[0]["id"]
            await db.execute(
                """INSERT INTO goals (area_id, horizon, content, display_order)
                   VALUES (?, '10yr', ?, 1)
                   ON CONFLICT DO NOTHING""",
                (area_id, content),
            )

    await db.commit()

    await update_step3(db)

    return success_redirect(request, "/onboarding?step=4", "Goals saved.")


@router.post("/onboarding/step/4")
async def save_step4(
    request: Request,
    training_routine: str = Form(""),
    equipment: list[str] = Form([]),  # noqa: B008
    habits_build: str = Form(""),
    habits_break: str = Form(""),
):
    if any(item not in ALLOWED_EQUIPMENT for item in equipment):
        return error_redirect(request, "/onboarding?step=4", "Choose only equipment options shown in the form.")

    db = get_user_db_from_request(request)
    await update_step4(
        db,
        training_routine=truncate(training_routine, 3000),
        equipment=",".join(equipment),
        habits_build=truncate(habits_build, 2000),
        habits_break=truncate(habits_break, 2000),
    )
    return success_redirect(request, "/onboarding?step=5", "Habits and training details saved.")


@router.post("/onboarding/step/5")
async def save_step5(
    request: Request,
    medical_text: str = Form(""),
    medical_file: UploadFile | None = File(None),  # noqa: B008
):
    """Stage the upload and queue the import.

    Two paid calls used to run here, one of them multimodal over a 20 MB PDF,
    while the browser waited. The bytes are staged instead and the job payload
    carries only an opaque token: a blood panel has no business in the jobs
    table, the status UI or the logs.
    """
    from app.config import INTERNAL_LLM_KEY
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key
    from app.services.medical_import import (
        MEDICAL_UPLOAD_MAX_BYTES,
        prune_staged,
        stage_upload,
    )

    db = get_user_db_from_request(request)
    raw_text = truncate(medical_text, MEDICAL_TEXT_MAX_CHARS)
    has_file = bool(medical_file and medical_file.filename)

    if not has_file and not raw_text.strip():
        return error_redirect(
            request,
            "/onboarding?step=5",
            "Upload a medical PDF or paste lab results, or skip this step.",
        )
    if not INTERNAL_LLM_KEY:
        return error_redirect(
            request,
            "/onboarding?step=5",
            "Medical record import requires an internal LLM key.",
        )

    if has_file:
        if medical_file.size is not None and medical_file.size > MEDICAL_UPLOAD_MAX_BYTES:
            return error_redirect(request, "/onboarding?step=5", "Medical PDFs must be 20 MB or smaller.")
        payload_bytes = await medical_file.read(MEDICAL_UPLOAD_MAX_BYTES + 1)
        if len(payload_bytes) > MEDICAL_UPLOAD_MAX_BYTES:
            return error_redirect(request, "/onboarding?step=5", "Medical PDFs must be 20 MB or smaller.")
        if not payload_bytes:
            return error_redirect(request, "/onboarding?step=5", "The uploaded medical PDF is empty.")
        source = "pdf"
    else:
        payload_bytes = raw_text.encode("utf-8")
        source = "text"

    user_id = request.state.user["id"]
    prune_staged(user_id)
    try:
        token = stage_upload(user_id, payload_bytes)
    except (OSError, ValueError):
        logger.exception("Could not stage the medical upload")
        return error_redirect(request, "/onboarding?step=5", "Could not accept that upload. Try again.")

    try:
        result = await enqueue_paid_llm_job(
            db,
            MEDICAL_JOB_KIND,
            {"source": source, "upload": token},
            idempotency_key=paid_llm_job_key(MEDICAL_JOB_KIND, token),
        )
    except ActiveWorkloadConflictError:
        _discard(user_id, token)
        return error_redirect(request, "/onboarding?step=5", "A medical import is already queued.")
    except Exception:
        logger.exception("Medical import enqueue failed")
        _discard(user_id, token)
        return error_redirect(request, "/onboarding?step=5", "The import could not be queued. Try again.")

    return success_redirect(
        request,
        f"/onboarding?step=6&job_id={result.job_id}",
        "Reading your results. They will appear in Bloodwork when the import finishes.",
    )


def _discard(user_id: str, token: str) -> None:
    from app.services.medical_import import discard_staged

    try:
        discard_staged(user_id, token)
    except (OSError, ValueError):
        logger.warning("Could not discard a staged medical upload")


@router.post("/onboarding/confirm")
async def confirm_onboarding(request: Request):
    """Finish onboarding now; the AI extras arrive on their own.

    Six sequential provider calls used to run inside this request. The account
    is usable without any of them, so completion no longer waits: the steps are
    queued, and the onboarding screen reports each one.
    """
    from app.auth import mark_onboarding_complete
    from app.config import INTERNAL_LLM_KEY
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key
    from app.services.onboarding import apply_feniks_trigger_words

    db = get_user_db_from_request(request)
    profile = await get_profile(db) or {}

    # A keyword match is free and deterministic, so it stays on the request path.
    if apply_feniks_trigger_words(profile):
        await set_setting(db, "feature_no_porn", "1")
        logger.info("Feniks feature auto-enabled based on onboarding habits")

    await set_setting(db, "onboarding_completed", "1")
    mark_onboarding_complete()

    if not INTERNAL_LLM_KEY:
        logger.warning("No VIRGIL_INTERNAL_LLM_KEY set - skipping LLM enrichment")
        return success_redirect(request, "/", "Onboarding complete.")

    try:
        result = await enqueue_paid_llm_job(
            db,
            ENRICHMENT_JOB_KIND,
            {},
            idempotency_key=paid_llm_job_key(ENRICHMENT_JOB_KIND, "confirm"),
        )
    except ActiveWorkloadConflictError:
        return success_redirect(request, "/", "Onboarding complete. The AI extras are already on their way.")
    except Exception:
        logger.exception("Onboarding enrichment enqueue failed")
        return success_redirect(request, "/", "Onboarding complete. The AI extras could not be queued.")
    return success_redirect(
        request,
        f"/?job_id={result.job_id}",
        "Onboarding complete. The AI extras are being prepared.",
    )


@router.post("/onboarding/enrichment/retry")
async def retry_enrichment(request: Request):
    """Buy the enrichment steps that are still missing, and only those."""
    from app.config import INTERNAL_LLM_KEY
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key

    if not INTERNAL_LLM_KEY:
        return error_redirect(request, "/onboarding?step=6", "AI extras need an internal LLM key.")
    db = get_user_db_from_request(request)
    nonce = secrets.token_hex(16)
    try:
        result = await enqueue_paid_llm_job(
            db,
            ENRICHMENT_JOB_KIND,
            {},
            idempotency_key=paid_llm_job_key(ENRICHMENT_JOB_KIND, nonce),
        )
    except ActiveWorkloadConflictError:
        return error_redirect(request, "/onboarding?step=6", "The AI extras are already queued.")
    except Exception:
        logger.exception("Onboarding enrichment retry failed")
        return error_redirect(request, "/onboarding?step=6", "The AI extras could not be queued. Try again.")
    return success_redirect(
        request,
        f"/onboarding?step=6&job_id={result.job_id}",
        "Finishing the AI extras.",
    )


@router.post("/onboarding/skip")
async def skip_onboarding(request: Request):
    db = get_user_db_from_request(request)
    await set_setting(db, "onboarding_completed", "1")

    from app.auth import mark_onboarding_complete

    mark_onboarding_complete()

    return success_redirect(request, "/", "Onboarding skipped.")
