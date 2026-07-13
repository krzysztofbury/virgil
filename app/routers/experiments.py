from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.user_db import get_user_db_from_request
from app.validation import truncate, valid_date

router = APIRouter(prefix="/experiments")

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_date(d: date) -> str:
    """Format date as 'Feb 18, 2026'."""
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _fmt_short(d: date) -> str:
    """Format date as 'Feb 18'."""
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _week_dates(start: date, week_num: int) -> tuple[date, date]:
    """Return (monday, sunday) for the given week number (1-based)."""
    monday = start + timedelta(weeks=week_num - 1)
    # Align to Monday if start isn't one
    monday = monday - timedelta(days=monday.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _build_week_grid(
    experiment: dict, activity_types: list[dict], weeks: list[dict], entries: list[dict]
) -> list[dict]:
    """Build the week-by-week grid data for the template."""
    today = date.today()
    start = date.fromisoformat(experiment["start_date"])
    # Align start to Monday
    start_monday = start - timedelta(days=start.weekday())

    # Index entries by date
    entries_by_date: dict[str, list[dict]] = {}
    for e in entries:
        entries_by_date.setdefault(e["date"], []).append(e)

    # Index weeks by week_number
    weeks_by_num = {w["week_number"]: w for w in weeks}

    # Activity type color map
    type_colors = {at["id"]: at["color"] for at in activity_types}
    type_names = {at["id"]: at["name"] for at in activity_types}

    grid = []
    for wn in range(1, experiment["num_weeks"] + 1):
        monday = start_monday + timedelta(weeks=wn - 1)
        sunday = monday + timedelta(days=6)
        week_cfg = weeks_by_num.get(wn, {"label": "", "target_min": 0, "target_max": 0})

        days = []
        week_total = 0
        week_entries_by_type: dict[int, int] = {}
        for d in range(7):
            day_date = monday + timedelta(days=d)
            day_str = day_date.isoformat()
            day_entries = entries_by_date.get(day_str, [])
            is_today = day_date == today

            # Sum durations for this day
            total_mins = sum(e["duration_minutes"] for e in day_entries)
            week_total += total_mins

            # Track per-type totals for status text
            for e in day_entries:
                tid = e["activity_type_id"]
                week_entries_by_type[tid] = week_entries_by_type.get(tid, 0) + e["duration_minutes"]

            # Pick dominant color for the cell (first entry's type)
            color = None
            label = ""
            if day_entries:
                color = type_colors.get(day_entries[0]["activity_type_id"])
                label = f"{total_mins}m"

            days.append(
                {
                    "date": day_str,
                    "is_today": is_today,
                    "entries": day_entries,
                    "total_mins": total_mins,
                    "color": color,
                    "label": label,
                }
            )

        # Progress calculation
        target_min = week_cfg["target_min"] or 0
        target_max = week_cfg["target_max"] or 0
        target_mid = (target_min + target_max) / 2 if target_max > 0 else 0
        progress_pct = min(100, round(week_total / target_mid * 100)) if target_mid > 0 else 0

        # Status text
        is_current = monday <= today <= sunday
        is_future = monday > today
        is_past = sunday < today

        if is_future:
            status = "upcoming"
            status_class = "muted"
        elif is_past or (is_current and week_total >= target_min):
            if week_total >= target_min:
                status = "complete"
                status_class = "success"
            else:
                remaining = target_min - week_total
                status = f"{remaining}m left"
                status_class = "warning"
        else:
            # Current week, in progress
            remaining = target_min - week_total
            # Build smart status: which types are missing
            parts = []
            if remaining > 0:
                parts.append(f"{remaining}m left")
            # Check which activity types have no entries this week
            for at in activity_types:
                if at["id"] not in week_entries_by_type:
                    parts.append(f"need {at['name']}")
            status = " · ".join(parts) if parts else f"{week_total}m logged"
            status_class = "active"

        grid.append(
            {
                "week_number": wn,
                "monday": monday.isoformat(),
                "sunday": sunday.isoformat(),
                "monday_fmt": _fmt_short(monday),
                "sunday_fmt": _fmt_short(sunday),
                "label": week_cfg.get("label") or "",
                "target_min": target_min,
                "target_max": target_max,
                "days": days,
                "total": week_total,
                "progress_pct": progress_pct,
                "status": status,
                "status_class": status_class,
                "is_current": is_current,
                "is_future": is_future,
                "per_type": {type_names.get(tid, "?"): mins for tid, mins in week_entries_by_type.items()},
            }
        )

    return grid


@router.get("", response_class=HTMLResponse)
async def experiments_list(request: Request):
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall(
        "SELECT * FROM experiments ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at DESC"
    )
    experiments = [dict(r) for r in rows]

    # Batch-load related data for all experiments
    today = date.today()
    exp_ids = [e["id"] for e in experiments]

    totals_by_exp: dict[int, int] = {}
    types_by_exp: dict[int, list[dict]] = {}
    targets_by_exp: dict[int, int] = {}

    if exp_ids:
        ph = ",".join("?" * len(exp_ids))

        # Total logged minutes per experiment
        total_rows = await db.execute_fetchall(
            f"SELECT experiment_id, COALESCE(SUM(duration_minutes), 0) as total "
            f"FROM experiment_entries WHERE experiment_id IN ({ph}) GROUP BY experiment_id",
            exp_ids,
        )
        totals_by_exp = {r["experiment_id"]: r["total"] for r in total_rows}

        # Activity types per experiment
        type_rows = await db.execute_fetchall(
            f"SELECT experiment_id, name, color FROM experiment_activity_types "
            f"WHERE experiment_id IN ({ph}) ORDER BY display_order",
            exp_ids,
        )
        for r in type_rows:
            types_by_exp.setdefault(r["experiment_id"], []).append(dict(r))

        # Target totals per experiment
        week_rows = await db.execute_fetchall(
            f"SELECT experiment_id, target_min, target_max FROM experiment_weeks WHERE experiment_id IN ({ph})",
            exp_ids,
        )
        for r in week_rows:
            targets_by_exp[r["experiment_id"]] = (
                targets_by_exp.get(r["experiment_id"], 0) + (r["target_min"] + r["target_max"]) // 2
            )

    for exp in experiments:
        start = date.fromisoformat(exp["start_date"])
        end = start + timedelta(weeks=exp["num_weeks"])
        exp["start_fmt"] = _fmt_date(start)
        exp["end_fmt"] = _fmt_date(end)
        elapsed_weeks = max(0, (today - start).days // 7)
        exp["weeks_done"] = min(elapsed_weeks, exp["num_weeks"])
        exp["current_week"] = max(1, min(exp["num_weeks"], (today - start).days // 7 + 1))
        exp["progress_pct"] = round(exp["weeks_done"] / exp["num_weeks"] * 100) if exp["num_weeks"] else 0
        exp["total_minutes"] = totals_by_exp.get(exp["id"], 0)
        exp["activity_types"] = types_by_exp.get(exp["id"], [])
        exp["target_total"] = targets_by_exp.get(exp["id"], 0)

    return templates.TemplateResponse("experiments.html", {"request": request, "experiments": experiments})


@router.get("/new", response_class=HTMLResponse)
async def new_experiment_form(request: Request):
    return templates.TemplateResponse("experiment_new.html", {"request": request, "today": date.today().isoformat()})


@router.post("/create")
async def create_experiment(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    start_date: str = Form(...),
    num_weeks: int = Form(...),
    target_min: int = Form(0),
    target_max: int = Form(0),
    activity_names: list[str] = Form(default=[]),  # noqa: B008
    activity_colors: list[str] = Form(default=[]),  # noqa: B008
    source_matches: list[str] = Form(default=[]),  # noqa: B008
    week_labels: str = Form(""),
):
    if not valid_date(start_date):
        return RedirectResponse("/experiments/new", status_code=303)
    if num_weeks < 1 or num_weeks > 52:
        return RedirectResponse("/experiments/new", status_code=303)
    title = truncate(title, 200)
    description = truncate(description, 2000)
    if not title.strip():
        return RedirectResponse("/experiments/new", status_code=303)
    # Normalize targets the same way week editing does — an inverted range
    # otherwise renders nonsense progress percentages.
    target_min = max(0, target_min)
    target_max = max(target_min, target_max)
    db = get_user_db_from_request(request)

    cursor = await db.execute(
        "INSERT INTO experiments (title, description, start_date, num_weeks) VALUES (?, ?, ?, ?)",
        (title.strip(), description, start_date, num_weeks),
    )
    exp_id = cursor.lastrowid

    # Create activity types
    for i, (name, color) in enumerate(zip(activity_names, activity_colors, strict=False)):
        if name.strip():
            sm = source_matches[i].strip() if i < len(source_matches) else ""
            await db.execute(
                "INSERT INTO experiment_activity_types (experiment_id, name, color, source_match, display_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (exp_id, name.strip(), color.strip(), sm, i + 1),
            )

    # Parse week labels (comma-separated: ",,DELOAD,,,,TAPER,")
    labels = [part.strip() for part in week_labels.split(",")] if week_labels.strip() else []

    # Create weeks with targets
    for wn in range(1, num_weeks + 1):
        label = labels[wn - 1] if wn - 1 < len(labels) else ""
        await db.execute(
            "INSERT INTO experiment_weeks (experiment_id, week_number, label, target_min, target_max) VALUES (?, ?, ?, ?, ?)",
            (exp_id, wn, label, target_min, target_max),
        )

    await db.commit()
    return RedirectResponse(f"/experiments/{exp_id}", status_code=303)


@router.get("/{experiment_id}", response_class=HTMLResponse)
async def experiment_detail(request: Request, experiment_id: int):
    db = get_user_db_from_request(request)

    rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not rows:
        return RedirectResponse("/experiments", status_code=303)
    experiment = dict(rows[0])

    activity_types = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types WHERE experiment_id = ? ORDER BY display_order",
            (experiment_id,),
        )
    ]

    weeks = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM experiment_weeks WHERE experiment_id = ? ORDER BY week_number",
            (experiment_id,),
        )
    ]

    entries = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT * FROM experiment_entries WHERE experiment_id = ? ORDER BY date",
            (experiment_id,),
        )
    ]

    grid = _build_week_grid(experiment, activity_types, weeks, entries)

    # Compute stats
    start = date.fromisoformat(experiment["start_date"])
    end = start + timedelta(weeks=experiment["num_weeks"])
    today = date.today()

    total_by_type: dict[int, int] = {}
    for e in entries:
        total_by_type[e["activity_type_id"]] = total_by_type.get(e["activity_type_id"], 0) + e["duration_minutes"]

    total_all = sum(total_by_type.values())
    target_total = sum((w.get("target_min", 0) + w.get("target_max", 0)) // 2 for w in weeks) if weeks else 0
    elapsed_weeks = max(0, min(experiment["num_weeks"], (today - start).days // 7))
    current_week = max(1, min(experiment["num_weeks"], (today - start).days // 7 + 1))

    # Type-level stats
    type_stats = []
    for at in activity_types:
        mins = total_by_type.get(at["id"], 0)
        type_stats.append({"name": at["name"], "color": at["color"], "total_minutes": mins})

    stats = {
        "total_minutes": total_all,
        "target_total": target_total,
        "weeks_done": elapsed_weeks,
        "current_week": current_week,
        "type_stats": type_stats,
    }

    # Check Oura connection
    oura_rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
    oura_connected = bool(oura_rows and oura_rows[0]["status"] == "connected")

    # LLM summaries — auto-generate for completed weeks
    from app.services.experiment_summary import (
        auto_generate_missing_summaries,
        get_existing_summaries,
        has_llm,
    )

    llm_available = await has_llm(db)
    await auto_generate_missing_summaries(db, experiment_id)
    summaries = await get_existing_summaries(db, experiment_id)

    return templates.TemplateResponse(
        "experiment_detail.html",
        {
            "request": request,
            "experiment": experiment,
            "activity_types": activity_types,
            "weeks": weeks,
            "entries": entries,
            "grid": grid,
            "stats": stats,
            "start_fmt": _fmt_date(start),
            "end_fmt": _fmt_date(end),
            "today": today.isoformat(),
            "oura_connected": oura_connected,
            "summaries": summaries,
            "llm_available": llm_available,
        },
    )


@router.post("/{experiment_id}/entry")
async def add_entry(
    request: Request,
    experiment_id: int,
    date: str = Form(...),
    activity_type_id: int = Form(...),
    duration_minutes: int = Form(...),
    notes: str = Form(""),
):
    if not valid_date(date):
        return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)
    if duration_minutes < 1:
        return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)
    notes = truncate(notes, 500)
    db = get_user_db_from_request(request)
    await db.execute(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, duration_minutes, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (experiment_id, date, activity_type_id, duration_minutes, notes),
    )
    await db.commit()
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/{experiment_id}/generate-summary")
async def generate_summary(
    request: Request,
    experiment_id: int,
    week_number: int = Form(...),
):
    from app.services.experiment_summary import generate_week_summary

    db = get_user_db_from_request(request)
    await generate_week_summary(db, experiment_id, week_number)
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/{experiment_id}/import-workouts")
async def import_workouts(request: Request, experiment_id: int):
    from app.services.oura_api import _auto_populate_experiments

    db = get_user_db_from_request(request)
    await _auto_populate_experiments(db)
    await db.commit()
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/{experiment_id}/delete-entry")
async def delete_entry(
    request: Request,
    experiment_id: int,
    entry_id: int = Form(...),
):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM experiment_entries WHERE id = ? AND experiment_id = ?", (entry_id, experiment_id))
    await db.commit()
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/{experiment_id}/complete")
async def complete_experiment(
    request: Request,
    experiment_id: int,
    new_status: str = Form("completed"),
):
    db = get_user_db_from_request(request)
    # 'active' allows undoing a mistaken Complete/Abandon click (reopen).
    if new_status in ("completed", "abandoned", "active"):
        await db.execute("UPDATE experiments SET status = ? WHERE id = ?", (new_status, experiment_id))
        await db.commit()
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)


@router.post("/{experiment_id}/delete")
async def delete_experiment(request: Request, experiment_id: int):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
    await db.commit()
    return RedirectResponse("/experiments", status_code=303)


@router.post("/{experiment_id}/week/{week_number}/targets")
async def update_week_targets(
    request: Request,
    experiment_id: int,
    week_number: int,
    target_min: int = Form(0),
    target_max: int = Form(0),
    label: str = Form(""),
):
    if target_min < 0:
        target_min = 0
    if target_max < target_min:
        target_max = target_min
    db = get_user_db_from_request(request)
    await db.execute(
        "UPDATE experiment_weeks SET target_min = ?, target_max = ?, label = ? "
        "WHERE experiment_id = ? AND week_number = ?",
        (target_min, target_max, label.strip(), experiment_id, week_number),
    )
    await db.commit()
    return RedirectResponse(f"/experiments/{experiment_id}", status_code=303)
