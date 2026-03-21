import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import set_setting
from app.main import templates
from app.models.user_profile import (
    ensure_profile,
    get_profile,
    update_step1,
    update_step2,
    update_step3,
    update_step4,
    update_step5,
)
from app.user_db import get_user_db_from_request
from app.validation import truncate

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB hard ceiling for medical PDFs.
MAX_MARKERS = 200
MAX_RESULTS_PER_MARKER = 50


def _safe_float(value: str) -> float | None:
    """Parse a form string to float, returning None on invalid input."""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return float(stripped)
    except ValueError:
        return None


GOAL_CATEGORY_MAP = {
    "mind": ["Duchowość", "Rozwój"],
    "body": ["Zdrowie"],
    "finance": ["Planowanie Życia", "Praca"],
    "relations": ["Rodzina", "Życie Towarzyskie", "Relaks"],
}


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request, step: int = 0):
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

    has_internal_llm = bool(INTERNAL_LLM_KEY)

    return templates.TemplateResponse(
        "onboarding.html",
        {
            "request": request,
            "step": step,
            "profile": profile,
            "goals": goals,
            "has_internal_llm": has_internal_llm,
        },
    )


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
    db = get_user_db_from_request(request)
    await update_step1(
        db,
        sex=truncate(sex, 20),
        age=int(age) if age.strip().isdigit() else None,
        height_cm=_safe_float(height_cm),
        weight_kg=_safe_float(weight_kg),
        family=truncate(family, 500),
        habits_good=truncate(habits_good, 2000),
        habits_bad=truncate(habits_bad, 2000),
    )
    return RedirectResponse("/onboarding?step=2", status_code=303)


@router.post("/onboarding/step/2")
async def save_step2(request: Request, ideal_day: str = Form("")):
    db = get_user_db_from_request(request)
    await update_step2(db, ideal_day=truncate(ideal_day, 5000))
    return RedirectResponse("/onboarding?step=3", status_code=303)


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

    return RedirectResponse("/onboarding?step=4", status_code=303)


@router.post("/onboarding/step/4")
async def save_step4(
    request: Request,
    training_routine: str = Form(""),
    equipment: list[str] = Form([]),  # noqa: B008
    habits_build: str = Form(""),
    habits_break: str = Form(""),
):
    db = get_user_db_from_request(request)
    await update_step4(
        db,
        training_routine=truncate(training_routine, 3000),
        equipment=",".join(equipment),
        habits_build=truncate(habits_build, 2000),
        habits_break=truncate(habits_break, 2000),
    )
    return RedirectResponse("/onboarding?step=5", status_code=303)


@router.post("/onboarding/step/5")
async def save_step5(
    request: Request,
    medical_text: str = Form(""),
    medical_file: UploadFile | None = File(None),  # noqa: B008
):
    db = get_user_db_from_request(request)
    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

    raw_text = truncate(medical_text, 10000)

    # Process PDF via multimodal LLM if uploaded.
    if medical_file and medical_file.size and medical_file.size > 0 and INTERNAL_LLM_KEY:
        if medical_file.size > MAX_UPLOAD_BYTES:
            return RedirectResponse("/onboarding?step=5", status_code=303)
        import base64

        import litellm

        pdf_bytes = await medical_file.read()
        pdf_b64 = base64.b64encode(pdf_bytes).decode()

        try:
            response = await litellm.acompletion(
                model=INTERNAL_LLM_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Extract all blood test markers from this PDF. "
                                    "Return ONLY a markdown list, one marker per line, format: "
                                    "'### Marker Name\\n* YYYY-MM-DD: value unit (flag)'. "
                                    "Include all dates found. Use (H) for high, (L) for low flags."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:application/pdf;base64,{pdf_b64}"},
                            },
                        ],
                    }
                ],
                api_key=INTERNAL_LLM_KEY,
                max_tokens=4096,
                timeout=120.0,
            )
            raw_text = response.choices[0].message.content
        except Exception:
            logger.exception("Failed to process medical PDF")

    # Parse extracted text into blood_markers + blood_results.
    if raw_text.strip() and INTERNAL_LLM_KEY:
        try:
            await _parse_medical_text(db, raw_text)
        except Exception:
            logger.exception("Failed to parse medical records")

    await update_step5(db)
    return RedirectResponse("/onboarding?step=6", status_code=303)


async def _parse_medical_text(db, text: str) -> None:
    """Use LLM to extract structured markers from free text, then save to DB."""
    import json

    import litellm

    from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL

    response = await litellm.acompletion(
        model=INTERNAL_LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "Extract blood test markers from the text. Return JSON array of objects: "
                    '{"marker": "name", "unit": "unit", "ref_low": number_or_null, "ref_high": number_or_null, '
                    '"results": [{"date": "YYYY-MM-DD", "value": number, "flag": "H"/"L"/null}]}. '
                    "Return ONLY valid JSON, no markdown fences."
                ),
            },
            {"role": "user", "content": text},
        ],
        api_key=INTERNAL_LLM_KEY,
        max_tokens=4096,
        timeout=90.0,
    )

    raw = response.choices[0].message.content.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    try:
        markers = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Could not parse medical markers JSON")
        return
    if not isinstance(markers, list):
        return
    markers = markers[:MAX_MARKERS]

    for m in markers:
        name = m.get("marker", "")
        if not name:
            continue
        unit = m.get("unit", "")
        ref_low = m.get("ref_low")
        ref_high = m.get("ref_high")

        # Upsert marker.
        await db.execute(
            """INSERT INTO blood_markers (name, unit, ref_low, ref_high, category)
               VALUES (?, ?, ?, ?, 'Imported')
               ON CONFLICT(name) DO UPDATE SET
                   unit = COALESCE(excluded.unit, unit),
                   ref_low = COALESCE(excluded.ref_low, ref_low),
                   ref_high = COALESCE(excluded.ref_high, ref_high)""",
            (name, unit, ref_low, ref_high),
        )

        marker_row = await db.execute_fetchall("SELECT id FROM blood_markers WHERE name = ?", (name,))
        if not marker_row:
            continue
        marker_id = marker_row[0]["id"]

        for r in m.get("results", [])[:MAX_RESULTS_PER_MARKER]:
            date_val = r.get("date", "")
            value = r.get("value")
            flag = r.get("flag", "")
            if date_val and value is not None:
                await db.execute(
                    """INSERT INTO blood_results (marker_id, date, value, flag)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT DO NOTHING""",
                    (marker_id, date_val, value, flag or ""),
                )

    await db.commit()


@router.post("/onboarding/confirm")
async def confirm_onboarding(request: Request):
    db = get_user_db_from_request(request)
    profile = await get_profile(db)

    # Run LLM enrichment (each part independent, only if data provided).
    from app.services.onboarding import run_enrichment

    await run_enrichment(db, profile)

    # Mark onboarding complete.
    await set_setting(db, "onboarding_completed", "1")

    from app.auth import mark_onboarding_complete

    mark_onboarding_complete()

    return RedirectResponse("/", status_code=303)


@router.post("/onboarding/skip")
async def skip_onboarding(request: Request):
    db = get_user_db_from_request(request)
    await set_setting(db, "onboarding_completed", "1")

    from app.auth import mark_onboarding_complete

    mark_onboarding_complete()

    return RedirectResponse("/", status_code=303)
