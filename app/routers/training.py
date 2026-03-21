import logging
from datetime import date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.main import templates
from app.validation import truncate, valid_date

logger = logging.getLogger(__name__)

router = APIRouter()

SECTION_ORDER = ["Warmup", "Core", "Cardio", "Stretching"]


@router.get("/training", response_class=HTMLResponse)
async def training_page(request: Request):
    db = await get_db()

    exercises = await db.execute_fetchall("SELECT * FROM training_exercises ORDER BY display_order")
    exercises = [dict(e) for e in exercises]

    # Group exercises by section, maintaining SECTION_ORDER
    sections: dict[str, list[dict]] = {s: [] for s in SECTION_ORDER}
    for ex in exercises:
        sec = ex["section"]
        if sec not in sections:
            sections[sec] = []
        sections[sec].append(ex)
    # Remove empty sections
    sections = {k: v for k, v in sections.items() if v}

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
               SUM(CASE WHEN tex.section = 'Core' THEN te.reps * COALESCE(te.weight, 0) ELSE 0 END) as core_volume,
               SUM(te.reps) as total_reps
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
        },
    )


@router.post("/training/session")
async def save_session(request: Request):
    db = await get_db()
    form = await request.form()

    session_date = form.get("date", date.today().isoformat())
    if not valid_date(session_date):
        return RedirectResponse("/training", status_code=303)
    duration = form.get("duration_minutes") or None
    try:
        duration_int = int(duration) if duration else None
    except (ValueError, TypeError):
        duration_int = None
    notes = truncate(form.get("session_notes", ""), 2000)

    cursor = await db.execute(
        "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, ?, ?)",
        (session_date, duration_int, notes),
    )
    session_id = cursor.lastrowid

    # Load exercises with their sections
    exercises = await db.execute_fetchall("SELECT id, section FROM training_exercises ORDER BY display_order")

    for ex in exercises:
        ex_id = ex["id"]
        section = ex["section"]

        if section in ("Warmup", "Stretching"):
            # Single entry with duration
            dur_key = f"exercise_{ex_id}_duration"
            done_key = f"exercise_{ex_id}_done"
            dur_val = form.get(dur_key)
            done_val = form.get(done_key)
            if dur_val or done_val:
                try:
                    dur_float = float(dur_val) if dur_val else None
                except (ValueError, TypeError):
                    dur_float = None
                await db.execute(
                    "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
                    "VALUES (?, ?, 1, NULL, NULL, ?)",
                    (session_id, ex_id, dur_float),
                )

        elif section == "Core":
            # Multi-set: reps + weight
            for set_num in range(1, 11):
                reps_key = f"exercise_{ex_id}_set_{set_num}_reps"
                weight_key = f"exercise_{ex_id}_set_{set_num}_weight"
                reps_val = form.get(reps_key)
                weight_val = form.get(weight_key)
                if reps_val:
                    try:
                        reps_int = int(reps_val)
                        weight_float = float(weight_val) if weight_val else None
                    except (ValueError, TypeError):
                        continue
                    await db.execute(
                        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, ex_id, set_num, reps_int, weight_float),
                    )

        elif section == "Cardio":
            # Multi-set: rounds + duration
            for set_num in range(1, 11):
                rounds_key = f"exercise_{ex_id}_set_{set_num}_reps"
                dur_key = f"exercise_{ex_id}_set_{set_num}_duration"
                rounds_val = form.get(rounds_key)
                dur_val = form.get(dur_key)
                if rounds_val or dur_val:
                    try:
                        rounds_int = int(rounds_val) if rounds_val else None
                        dur_float = float(dur_val) if dur_val else None
                    except (ValueError, TypeError):
                        continue
                    await db.execute(
                        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration) "
                        "VALUES (?, ?, ?, ?, NULL, ?)",
                        (session_id, ex_id, set_num, rounds_int, dur_float),
                    )

    await db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/session/{session_id}/delete")
async def delete_session(session_id: int):
    db = await get_db()
    await db.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    await db.commit()
    return RedirectResponse("/training", status_code=303)


# --- Exercise CRUD ---


@router.post("/training/exercise")
async def add_exercise(request: Request):
    db = await get_db()
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
    db = await get_db()
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
async def delete_exercise(exercise_id: int):
    db = await get_db()
    # Delete entries referencing this exercise first
    await db.execute("DELETE FROM training_entries WHERE exercise_id = ?", (exercise_id,))
    await db.execute("DELETE FROM training_exercises WHERE id = ?", (exercise_id,))
    await db.commit()
    return RedirectResponse("/training", status_code=303)
