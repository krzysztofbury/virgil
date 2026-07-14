import os
from datetime import date, timedelta

from app.config import SECOND_BRAIN_PATH
from app.db import get_setting


def _status_to_checkbox(status: str) -> str:
    return {"done": "[x]", "skipped": "[-]", "pending": "[ ]"}.get(status, "[ ]")


def _fmt_val(val) -> str:
    """Format a numeric value: drop .0 from floats."""
    if val is None:
        return ""
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _date_range_for_scope(scope: str) -> tuple[str, str]:
    """Return (start_date, end_date) ISO strings for the given scope."""
    today = date.today()
    if scope == "weekly":
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), sunday.isoformat()
    elif scope == "monthly":
        start = today.replace(day=1)
        # Last day of month
        if today.month == 12:
            end = today.replace(year=today.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            end = today.replace(month=today.month + 1, day=1) - timedelta(days=1)
        return start.isoformat(), end.isoformat()
    elif scope == "yearly":
        return f"{today.year}-01-01", f"{today.year}-12-31"
    else:  # all
        return "0000-01-01", "9999-12-31"


async def _section_daily_logs(db, start: str, end: str) -> list[str]:
    rows = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))
    if not rows:
        return []
    lines = ["## Daily Logs", ""]
    for row in rows:
        r = dict(row)
        energy = r.get("energy", "?")
        energy_str = f"{energy}/10" if energy and energy != "?" else "?/10"
        lines.append(f"### {r['date']}")
        lines.append(f"- Energy: {energy_str}")
        lines.append(f"- Morning Routine: {_status_to_checkbox(r.get('morning_routine', 'pending'))}")
        lines.append(f"- Evening Routine: {_status_to_checkbox(r.get('evening_routine', 'pending'))}")
        lines.append(f"- Water: {_status_to_checkbox(r.get('water', 'pending'))}")
        lines.append(
            f"- A.N.D.Y. Body: {_status_to_checkbox(r.get('andy_body_status', 'pending'))} {r.get('andy_body_desc', '')}"
        )
        lines.append(
            f"- A.N.D.Y. Spirit: {_status_to_checkbox(r.get('andy_spirit_status', 'pending'))} {r.get('andy_spirit_desc', '')}"
        )
        lines.append(
            f"- A.N.D.Y. Account: {_status_to_checkbox(r.get('andy_account_status', 'pending'))} {r.get('andy_account_desc', '')}"
        )
        lines.append(
            f"- A.N.D.Y. Relations: {_status_to_checkbox(r.get('andy_relations_status', 'pending'))} {r.get('andy_relations_desc', '')}"
        )
        if r.get("notes"):
            lines.append(f"- Notes: {r['notes']}")
        lines.append("")
    return lines


async def _section_training(db, start: str, end: str) -> list[str]:
    sessions = await db.execute_fetchall(
        "SELECT * FROM training_sessions WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    )
    if not sessions:
        return []

    # Batch-load all entries for these sessions
    session_ids = [s["id"] for s in sessions]
    ph = ",".join("?" * len(session_ids))
    all_entries = await db.execute_fetchall(
        f"""SELECT te.*, tex.name as exercise_name
           FROM training_entries te
           JOIN training_exercises tex ON te.exercise_id = tex.id
           WHERE te.session_id IN ({ph})
           ORDER BY tex.display_order, te.set_number""",
        session_ids,
    )
    entries_by_session: dict[int, list[dict]] = {}
    for e in all_entries:
        entries_by_session.setdefault(e["session_id"], []).append(dict(e))

    lines = ["## Training Sessions", ""]
    for s in sessions:
        s = dict(s)
        dur = f" ({s['duration_minutes']} min)" if s["duration_minutes"] else ""
        lines.append(f"### {s['date']}{dur}")
        if s["notes"]:
            lines.append(f"> {s['notes']}")
        entries = entries_by_session.get(s["id"], [])
        if entries:
            lines.append("| Exercise | Set | Reps | Weight |")
            lines.append("|----------|-----|------|--------|")
            for e in entries:
                w = f"{_fmt_val(e['weight'])} kg" if e["weight"] else "-"
                lines.append(f"| {e['exercise_name']} | {e['set_number']} | {e['reps'] or '-'} | {w} |")
        lines.append("")
    return lines


async def _section_body_measurements(db, start: str, end: str) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT * FROM body_measurements WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    )
    if not rows:
        return []
    lines = ["## Body Measurements", ""]
    for r in rows:
        r = dict(r)
        lines.append(f"### {r['date']}")
        for field, label in [
            ("weight", "Weight"),
            ("arm", "Arm"),
            ("waist", "Waist"),
            ("hips", "Hips"),
            ("thighs", "Thighs"),
        ]:
            val = r.get(field)
            if val is not None:
                lines.append(f"- {label}: {_fmt_val(val)}")
        lines.append("")
    return lines


async def _section_feniks(db, start: str, end: str) -> list[str]:
    journal = await db.execute_fetchall(
        "SELECT * FROM feniks_journal WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    )
    pleasures = await db.execute_fetchall(
        "SELECT * FROM feniks_pleasures WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    )
    relapses = await db.execute_fetchall(
        "SELECT * FROM pmo_events WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
    )
    if not journal and not pleasures and not relapses:
        return []

    # Streak info
    from app.services.streak import get_streak

    streak_days, _ = await get_streak(db)

    lines = ["## No Porn", "", f"**Current streak:** {streak_days} days", ""]

    if journal:
        lines.append("### Journal")
        lines.append("| Date | Emotions | Triggers | Thoughts | Desired Feelings | Coping Strategies |")
        lines.append("|------|----------|----------|----------|------------------|-------------------|")
        for j in journal:
            j = dict(j)
            lines.append(
                f"| {j['date']} | {j.get('emotions', '')} | {j.get('triggers', '')} | "
                f"{j.get('thoughts', '')} | {j.get('desired_feelings', '')} | {j.get('coping_strategies', '')} |"
            )
        lines.append("")

    if pleasures:
        lines.append("### Pleasures")
        lines.append("| Date | Pleasure 1 | Pleasure 2 |")
        lines.append("|------|------------|------------|")
        for p in pleasures:
            p = dict(p)
            lines.append(f"| {p['date']} | {p.get('pleasure_1', '')} | {p.get('pleasure_2', '')} |")
        lines.append("")

    if relapses:
        lines.append("### Relapses")
        for r in relapses:
            r = dict(r)
            notes = f" - {r['notes']}" if r.get("notes") else ""
            lines.append(f"- {r['date']}: {r['event_type']}{notes}")
        lines.append("")

    return lines


async def _section_oura(db, start: str, end: str) -> list[str]:
    # Oura months are "YYYY-MM" — filter by month prefix range
    start_month = start[:7]
    end_month = end[:7]
    rows = await db.execute_fetchall(
        "SELECT * FROM oura_monthly WHERE month BETWEEN ? AND ? ORDER BY month",
        (start_month, end_month),
    )
    data = [dict(r) for r in rows]
    if not data:
        return []

    lines = ["## Oura Ring Data", ""]
    metrics = [
        ("sleep_score", "Sleep Score", ""),
        ("readiness", "Readiness", ""),
        ("activity", "Activity", ""),
        ("steps", "Steps", ""),
        ("sleep_duration", "Sleep Duration", " h"),
        ("deep_sleep", "Deep Sleep", " h"),
        ("rem_sleep", "REM Sleep", " h"),
        ("rhr", "Resting HR", " bpm"),
        ("lowest_hr", "Lowest HR", " bpm"),
        ("hrv", "HRV", " ms"),
        ("cardiovascular_age", "Cardio Age", ""),
    ]
    for key, title, unit in metrics:
        vals = [(d["month"], d.get(key)) for d in data if d.get(key) is not None]
        if vals:
            lines.append(f"### {title}")
            for month, val in vals:
                lines.append(f"* {month}: {_fmt_val(val)}{unit}")
            lines.append("")

    stress_data = [d for d in data if d.get("stress_normal") is not None]
    if stress_data:
        lines.append("### Stress")
        for d in stress_data:
            lines.append(
                f"* {d['month']}: {d.get('stress_normal', 0)} normal, "
                f"{d.get('stress_stressful', 0)} stressful, "
                f"{d.get('stress_restored', 0)} restored"
            )
        lines.append("")

    return lines


async def _section_life_scores(db, start: str, end: str) -> list[str]:
    rows = await db.execute_fetchall(
        "SELECT * FROM life_scores WHERE date BETWEEN ? AND ? ORDER BY date DESC", (start, end)
    )
    if not rows:
        return []

    lines = ["## Life Scores", ""]
    area_map = [
        ("planning", "Planning"),
        ("spirituality", "Spirituality"),
        ("health", "Health"),
        ("work", "Work"),
        ("social", "Social"),
        ("growth", "Growth"),
        ("relaxation", "Relaxation"),
        ("family", "Family"),
    ]
    for s in rows:
        s = dict(s)
        lines.append(f"### {s['date']} (Power Level: {_fmt_val(s.get('power_level'))})")
        for key, label in area_map:
            val = s.get(key)
            if val is not None:
                lines.append(f"- {label}: {val}/10")
        if s.get("diagnostic"):
            lines.append(f"- Diagnostic: {s['diagnostic']}")
        if s.get("priorities"):
            lines.append(f"- Priorities: {s['priorities']}")
        lines.append("")
    return lines


async def _section_experiments(db, start: str, end: str) -> list[str]:
    experiments = await db.execute_fetchall(
        "SELECT * FROM experiments WHERE start_date <= ? AND status = 'active' ORDER BY start_date", (end,)
    )
    if not experiments:
        return []

    exp_ids = [r["id"] for r in experiments]
    ph = ",".join("?" * len(exp_ids))

    # Batch-load entries and summaries for all experiments
    all_entries = await db.execute_fetchall(
        f"""SELECT ee.experiment_id, ee.date, ee.duration_minutes, ee.notes, eat.name as activity_name
           FROM experiment_entries ee
           JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id
           WHERE ee.experiment_id IN ({ph}) AND ee.date BETWEEN ? AND ?
           ORDER BY ee.date""",
        [*exp_ids, start, end],
    )
    entries_by_exp: dict[int, list[dict]] = {}
    for row in all_entries:
        entries_by_exp.setdefault(row["experiment_id"], []).append(dict(row))

    all_summaries = await db.execute_fetchall(
        f"SELECT * FROM experiment_summaries WHERE experiment_id IN ({ph}) ORDER BY week_number",
        exp_ids,
    )
    summaries_by_exp: dict[int, list[dict]] = {}
    for row in all_summaries:
        summaries_by_exp.setdefault(row["experiment_id"], []).append(dict(row))

    lines = ["## Experiments", ""]
    for row in experiments:
        exp = dict(row)
        lines.append(f"### {exp['title']}")
        if exp.get("description"):
            lines.append(f"{exp['description']}")
        lines.append(f"- Start: {exp['start_date']}, Duration: {exp['num_weeks']} weeks")

        entries = entries_by_exp.get(exp["id"], [])
        if entries:
            lines.append("")
            lines.append("| Date | Activity | Duration | Notes |")
            lines.append("|------|----------|----------|-------|")
            for e in entries:
                notes = e.get("notes", "") or ""
                lines.append(f"| {e['date']} | {e['activity_name']} | {e['duration_minutes']} min | {notes} |")

        summaries = summaries_by_exp.get(exp["id"], [])
        if summaries:
            lines.append("")
            for s in summaries:
                lines.append(f"**Week {s['week_number']}:** {s['summary']}")

        lines.append("")
    return lines


async def _section_bloodwork(db, start: str, end: str) -> list[str]:
    markers = await db.execute_fetchall("SELECT * FROM blood_markers ORDER BY display_order, name")
    if not markers:
        return []

    # Batch-load all results in date range
    marker_ids = [m["id"] for m in markers]
    ph = ",".join("?" * len(marker_ids))
    all_results = await db.execute_fetchall(
        f"SELECT * FROM blood_results WHERE marker_id IN ({ph}) AND date BETWEEN ? AND ? ORDER BY date",
        [*marker_ids, start, end],
    )
    if not all_results:
        return []

    results_by_marker: dict[int, list[dict]] = {}
    for r in all_results:
        results_by_marker.setdefault(r["marker_id"], []).append(dict(r))

    lines = ["## Blood Work", ""]
    current_category = None
    for m in markers:
        m = dict(m)
        marker_results = results_by_marker.get(m["id"], [])
        if not marker_results:
            continue

        if m["category"] != current_category:
            if current_category is not None:
                lines.append("")
            current_category = m["category"]

        lines.append(f"### {m['name']}")
        for r in marker_results:
            text = r.get("value_text", "")
            flag_str = f" ({r['flag']})" if r.get("flag") else ""
            val_display = text if text else _fmt_val(r["value"])
            lines.append(f"* {r['date']}: {val_display} {m['unit']}{flag_str}")
        lines.append("")
    return lines


async def _section_goals(db) -> list[str]:
    areas = await db.execute_fetchall("SELECT * FROM goal_areas ORDER BY display_order")
    goals = await db.execute_fetchall("SELECT * FROM goals ORDER BY area_id, horizon, display_order")
    if not goals:
        return []

    goals_map: dict[tuple[int, str], list[dict]] = {}
    for g in goals:
        g = dict(g)
        key = (g["area_id"], g["horizon"])
        goals_map.setdefault(key, []).append(g)

    today = date.today()
    horizon_labels = {
        "1yr": f"1 Year ({today.year})",
        "3yr": f"3 Years ({today.year + 2})",
        "10yr": f"10 Years ({today.year + 9})",
    }

    lines = ["## Goals", ""]
    for a in areas:
        a = dict(a)
        area_has_goals = any(goals_map.get((a["id"], h)) for h in horizon_labels)
        if not area_has_goals:
            continue
        lines.append(f"### {a.get('icon', '')} {a['name']}")
        for horizon, h_label in horizon_labels.items():
            area_goals = goals_map.get((a["id"], horizon), [])
            if area_goals:
                lines.append(f"**{h_label}:**")
                for g in area_goals:
                    lines.append(f"- {g['content']}")
                lines.append("")
    return lines


async def export_markdown(db, scope: str = "weekly", sections: set[str] | None = None) -> str:
    """Generate a single markdown document with all Virgil data for the given scope.

    If sections is None, include all sections (backward compat).
    Otherwise only include the named sections.
    """
    start, end = _date_range_for_scope(scope)
    today = date.today()
    feniks_enabled = await get_setting(db, "feature_no_porn", "0") == "1"

    def _include(name: str) -> bool:
        return sections is None or name in sections

    lines = [
        f"# Virgil Export ({scope})",
        f"**Generated:** {today.isoformat()}",
        f"**Scope:** {start} to {end}",
        "",
        "---",
        "",
    ]

    # Weekly sections
    if _include("daily_logs"):
        lines.extend(await _section_daily_logs(db, start, end))
    if _include("training"):
        lines.extend(await _section_training(db, start, end))
    if _include("body_measurements"):
        lines.extend(await _section_body_measurements(db, start, end))
    if _include("feniks") and feniks_enabled:
        lines.extend(await _section_feniks(db, start, end))

    # Monthly+ sections
    if scope in ("monthly", "yearly", "all"):
        if _include("oura"):
            lines.extend(await _section_oura(db, start, end))
        if _include("life_scores"):
            lines.extend(await _section_life_scores(db, start, end))
        if _include("experiments"):
            lines.extend(await _section_experiments(db, start, end))

    # Yearly+ sections
    if scope in ("yearly", "all"):
        if _include("bloodwork"):
            lines.extend(await _section_bloodwork(db, start, end))
        if _include("goals"):
            lines.extend(await _section_goals(db))

    lines.append("---")
    return "\n".join(lines) + "\n"


_EXPORT_FILENAME_MAX_LEN = 100


def valid_export_filename(filename: str) -> bool:
    """A plain markdown filename — no path traversal into the second brain."""
    if not filename or len(filename) > _EXPORT_FILENAME_MAX_LEN:
        return False
    if "/" in filename or "\\" in filename or ".." in filename:
        return False
    if filename.startswith("."):
        return False
    return filename.endswith(".md") and len(filename) > len(".md")


async def export_filename_for(db, user_id: str) -> str:
    """Per-user export filename, DERIVED from identity — never user-chosen.

    All users share one SECOND_BRAIN_PATH; a free-form filename setting would
    let one user select (and overwrite, and leak into) another user's export.
    The primary (oldest) account keeps the legacy `virgil.md` for existing
    single-user integrations (OpenClaw); every other account gets a name
    derived from its immutable id.
    """
    from app.central_db import get_primary_user_id

    assert user_id, "export_filename_for requires a user id"
    if user_id == await get_primary_user_id():
        return "virgil.md"
    return f"virgil-{user_id[:8]}.md"


async def write_export(db, scope: str = "weekly", sections: set[str] | None = None, filename: str = "virgil.md") -> str:
    """Generate and write the markdown export to SECOND_BRAIN_PATH/filename.
    Returns the content.

    Logs success/error to sync_log table.
    """
    assert valid_export_filename(filename), f"Unsafe export filename: {filename!r}"
    content = await export_markdown(db, scope, sections=sections)

    if SECOND_BRAIN_PATH:
        path = os.path.join(SECOND_BRAIN_PATH, filename)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, path)
        except OSError as exc:
            await db.execute(
                "INSERT INTO sync_log (file_name, status, message) VALUES (?, 'error', ?)",
                (filename, str(exc)),
            )
            await db.commit()
            raise

    await db.execute(
        "INSERT INTO sync_log (file_name, status, message) VALUES (?, 'success', ?)",
        (filename, f"Exported scope: {scope}"),
    )
    await db.commit()
    return content
