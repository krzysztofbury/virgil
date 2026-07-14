import logging
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import truncate, valid_date

logger = logging.getLogger(__name__)

router = APIRouter()

SECTION_ORDER = ["Warmup", "Core", "Cardio", "Stretching"]

# Server-side sanity bounds — the client can send anything.
MAX_SETS_PER_EXERCISE = 10
REPS_MAX = 1000
WEIGHT_KG_MAX = 1000.0
DURATION_MINUTES_MAX = 1440.0
DURATION_SECONDS_MAX = 86400.0


def _parse_int_in_range(raw, minimum: int, maximum: int) -> int | None:
    """Parse a form value as int within [minimum, maximum]; None if invalid."""
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    if value < minimum or value > maximum:
        return None
    return value


def _parse_float_in_range(raw, minimum: float, maximum: float) -> float | None:
    """Parse a form value as float within [minimum, maximum]; None if invalid."""
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return None
    if value < minimum or value > maximum:
        return None
    return value


@router.get("/training", response_class=HTMLResponse)
async def training_page(request: Request):
    db = get_user_db_from_request(request)

    # Archived exercises stay out of the protocol/log forms but keep their
    # historical entries (session history and PBs join by id regardless).
    exercises = await db.execute_fetchall("SELECT * FROM training_exercises WHERE archived = 0 ORDER BY display_order")
    exercises = [dict(e) for e in exercises]

    # Group exercises by section, maintaining SECTION_ORDER
    sections: dict[str, list[dict]] = {s: [] for s in SECTION_ORDER}
    for ex in exercises:
        sec = ex["section"]
        if sec not in sections:
            sections[sec] = []
        sections[sec].append(ex)
    # Keep empty sections visible so the per-section "Add exercise" form
    # remains reachable after the last exercise in a section is deleted.

    sessions = await db.execute_fetchall("SELECT * FROM training_sessions ORDER BY date DESC LIMIT 20")
    sessions = [dict(s) for s in sessions]

    # Load all entries for visible sessions in one query
    if sessions:
        session_ids = [s["id"] for s in sessions]
        placeholders = ",".join("?" * len(session_ids))
        all_entries = await db.execute_fetchall(
            f"""SELECT te.*, tex.name as exercise_name, tex.section
               FROM training_entries te
               JOIN training_exercises tex ON te.exercise_id = tex.id
               WHERE te.session_id IN ({placeholders})
               ORDER BY tex.display_order, te.set_number""",
            session_ids,
        )
        entries_by_session: dict[int, list[dict]] = {}
        for e in all_entries:
            entries_by_session.setdefault(e["session_id"], []).append(dict(e))
        for s in sessions:
            s["entries"] = entries_by_session.get(s["id"], [])
    else:
        for s in sessions:
            s["entries"] = []

    # --- KPIs: This Week ---
    today = date.today()
    # Monday of current week
    monday = today - timedelta(days=today.weekday())
    monday_str = monday.isoformat()

    # Session count (simple count, no join inflation)
    session_count_row = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM training_sessions WHERE date >= ?",
        (monday_str,),
    )
    kpi_sessions = session_count_row[0]["cnt"] if session_count_row else 0

    # Volume (Core only) and total reps (all sections)
    week_stats = await db.execute_fetchall(
        """SELECT
               SUM(CASE WHEN tex.section = 'Core' AND tex.metric = 'reps'
                        THEN te.reps * COALESCE(te.weight, 0) ELSE 0 END) as core_volume,
               SUM(CASE WHEN tex.metric = 'reps' THEN te.reps ELSE 0 END) as total_reps
           FROM training_entries te
           JOIN training_sessions ts ON te.session_id = ts.id
           JOIN training_exercises tex ON te.exercise_id = tex.id
           WHERE ts.date >= ?""",
        (monday_str,),
    )
    kpi_volume = 0
    kpi_reps = 0
    if week_stats:
        row = week_stats[0]
        kpi_volume = round(row["core_volume"] or 0)
        kpi_reps = row["total_reps"] or 0

    # --- Personal Bests (last 12 weeks, Core exercises only) ---
    twelve_weeks_ago = (today - timedelta(weeks=12)).isoformat()
    pb_rows = await db.execute_fetchall(
        """SELECT tex.name, MAX(te.weight) as max_weight
           FROM training_entries te
           JOIN training_sessions ts ON te.session_id = ts.id
           JOIN training_exercises tex ON te.exercise_id = tex.id
           WHERE ts.date >= ? AND tex.section = 'Core' AND te.weight > 0
           GROUP BY tex.id
           ORDER BY tex.display_order""",
        (twelve_weeks_ago,),
    )
    personal_bests = [dict(r) for r in pb_rows]

    # Exercise picker dictionary — DB-backed and user-editable (seeded by migration 009).
    lib_rows = await db.execute_fetchall(
        "SELECT category, section, name, sets, reps, notes FROM exercise_library ORDER BY display_order, name"
    )
    exercise_library = [dict(r) for r in lib_rows]

    return templates.TemplateResponse(
        "training.html",
        {
            "request": request,
            "exercises": exercises,
            "sections": sections,
            "sessions": sessions,
            "today": today.isoformat(),
            "kpi_sessions": kpi_sessions,
            "kpi_volume": kpi_volume,
            "kpi_reps": kpi_reps,
            "personal_bests": personal_bests,
            "section_order": SECTION_ORDER,
            "exercise_library": exercise_library,
        },
    )


@router.post("/training/session")
async def save_session(request: Request):
    db = get_user_db_from_request(request)
    form = await request.form()

    session_date = form.get("date", date.today().isoformat())
    if not valid_date(session_date):
        return RedirectResponse("/training", status_code=303)
    duration_int = _parse_int_in_range(form.get("duration_minutes"), 1, int(DURATION_MINUTES_MAX))
    notes = truncate(form.get("session_notes", ""), 2000)

    # Collect validated entries FIRST — the session row is only created when the
    # workout actually contains something, so a stray submit can't pollute
    # history/KPIs with empty sessions. Out-of-range values are skipped.
    exercises = await db.execute_fetchall(
        "SELECT id, section, metric FROM training_exercises WHERE archived = 0 ORDER BY display_order"
    )
    entries: list[tuple] = []  # (exercise_id, set_number, reps, weight, duration)

    for ex in exercises:
        ex_id = ex["id"]
        section = ex["section"]
        metric = ex["metric"]

        if section in ("Warmup", "Stretching"):
            # Single entry with duration
            dur_val = form.get(f"exercise_{ex_id}_duration")
            done_val = form.get(f"exercise_{ex_id}_done")
            if dur_val or done_val:
                dur_float = _parse_float_in_range(dur_val, 0.0, DURATION_MINUTES_MAX) if dur_val else None
                if dur_val and dur_float is None and not done_val:
                    continue
                entries.append((ex_id, 1, None, None, dur_float))

        elif section == "Core" and metric == "time":
            # Weighted hold/carry: weight + seconds (reps NULL → excluded from volume/reps)
            for set_num in range(1, MAX_SETS_PER_EXERCISE + 1):
                sec_val = form.get(f"exercise_{ex_id}_set_{set_num}_seconds")
                weight_val = form.get(f"exercise_{ex_id}_set_{set_num}_weight")
                if not sec_val and not weight_val:
                    continue
                sec_float = _parse_float_in_range(sec_val, 0.0, DURATION_SECONDS_MAX) if sec_val else None
                weight_float = _parse_float_in_range(weight_val, 0.0, WEIGHT_KG_MAX) if weight_val else None
                if sec_float is None and weight_float is None:
                    continue
                entries.append((ex_id, set_num, None, weight_float, sec_float))

        elif section == "Core":
            # Multi-set: reps + weight
            for set_num in range(1, MAX_SETS_PER_EXERCISE + 1):
                reps_val = form.get(f"exercise_{ex_id}_set_{set_num}_reps")
                if not reps_val:
                    continue
                reps_int = _parse_int_in_range(reps_val, 1, REPS_MAX)
                if reps_int is None:
                    continue
                weight_val = form.get(f"exercise_{ex_id}_set_{set_num}_weight")
                weight_float = _parse_float_in_range(weight_val, 0.0, WEIGHT_KG_MAX) if weight_val else None
                entries.append((ex_id, set_num, reps_int, weight_float, None))

        elif section == "Cardio":
            # Multi-set: rounds + duration
            for set_num in range(1, MAX_SETS_PER_EXERCISE + 1):
                rounds_val = form.get(f"exercise_{ex_id}_set_{set_num}_reps")
                dur_val = form.get(f"exercise_{ex_id}_set_{set_num}_duration")
                if not rounds_val and not dur_val:
                    continue
                rounds_int = _parse_int_in_range(rounds_val, 1, REPS_MAX) if rounds_val else None
                dur_float = _parse_float_in_range(dur_val, 0.0, DURATION_MINUTES_MAX) if dur_val else None
                if rounds_int is None and dur_float is None:
                    continue
                entries.append((ex_id, set_num, rounds_int, None, dur_float))

    if not entries and not notes and duration_int is None:
        # Nothing was logged — don't create an empty session.
        return RedirectResponse("/training", status_code=303)

    cursor = await db.execute(
        "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, ?, ?)",
        (session_date, duration_int, notes),
    )
    session_id = cursor.lastrowid

    await db.executemany(
        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(session_id, ex_id, set_num, reps, weight, duration) for ex_id, set_num, reps, weight, duration in entries],
    )

    await db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/session/{session_id}/delete")
async def delete_session(request: Request, session_id: int):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    await db.commit()
    return RedirectResponse("/training", status_code=303)


# --- Exercise CRUD ---


@router.post("/training/exercise")
async def add_exercise(request: Request):
    db = get_user_db_from_request(request)
    form = await request.form()

    name = truncate(form.get("name", "").strip(), 100)
    section = form.get("section", "Core")
    if section not in SECTION_ORDER:
        section = "Core"
    try:
        target_sets = int(form.get("target_sets", 3))
    except (ValueError, TypeError):
        target_sets = 3
    target_reps = truncate(form.get("target_reps", "").strip(), 50)
    notes = truncate(form.get("notes", "").strip(), 200)

    if not name:
        return RedirectResponse("/training", status_code=303)

    # Get next display_order
    row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as mx FROM training_exercises")
    next_order = row[0]["mx"] + 1

    await db.execute(
        "INSERT INTO training_exercises (name, section, target_sets, target_reps, notes, display_order) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, section, target_sets, target_reps, notes, next_order),
    )
    await db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/exercise/{exercise_id}/edit")
async def edit_exercise(exercise_id: int, request: Request):
    db = get_user_db_from_request(request)
    form = await request.form()

    name = truncate(form.get("name", "").strip(), 100)
    section = form.get("section", "Core")
    if section not in SECTION_ORDER:
        section = "Core"
    try:
        target_sets = int(form.get("target_sets", 3))
    except (ValueError, TypeError):
        target_sets = 3
    target_reps = truncate(form.get("target_reps", "").strip(), 50)
    notes = truncate(form.get("notes", "").strip(), 200)

    if not name:
        return RedirectResponse("/training", status_code=303)

    await db.execute(
        "UPDATE training_exercises SET name=?, section=?, target_sets=?, target_reps=?, notes=? WHERE id=?",
        (name, section, target_sets, target_reps, notes, exercise_id),
    )
    await db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/exercise/{exercise_id}/delete")
async def delete_exercise(request: Request, exercise_id: int):
    """Archive an exercise that has history; hard-delete only unused ones.

    Hard-deleting used to cascade through every historical training entry —
    one click silently destroyed months of workout history and PBs.
    """
    db = get_user_db_from_request(request)
    entry_rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM training_entries WHERE exercise_id = ?", (exercise_id,)
    )
    if entry_rows[0]["n"] > 0:
        await db.execute("UPDATE training_exercises SET archived = 1 WHERE id = ?", (exercise_id,))
    else:
        await db.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
    await db.commit()
    return RedirectResponse("/training", status_code=303)
