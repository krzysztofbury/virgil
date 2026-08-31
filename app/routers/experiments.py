import logging
from datetime import date, timedelta

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.experiment_weeks import experiment_calendar, experiment_week_number
from app.user_db import get_user_db_from_request
from app.validation import (
    METRIC_KINDS,
    TARGET_PERIODS,
    OptionalFormInt,
    clamp_metric_value,
    truncate,
    valid_date,
)

router = APIRouter(prefix="/experiments")
logger = logging.getLogger(__name__)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _fmt_date(d: date) -> str:
    """Format date as 'Feb 18, 2026'."""
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _fmt_short(d: date) -> str:
    """Format date as 'Feb 18'."""
    return f"{_MONTHS[d.month - 1]} {d.day}"


def _normalize_metric(name: str, color: str, kind: str, target: str, period: str, source_match: str) -> dict | None:
    """Validate one metric row from the create/edit forms. None = skip row."""
    if not name.strip():
        return None
    kind = kind if kind in METRIC_KINDS else "duration"
    period = period if period in TARGET_PERIODS else "week"
    try:
        target_value = max(0, int(target))
    except (TypeError, ValueError):
        target_value = 0
    if kind not in ("count", "boolean"):
        target_value = 0  # targets only defined for count/boolean metrics
    if kind == "boolean" and period == "day":
        target_value = min(target_value, 1)  # "every day" — 1 is the only sane daily bar
    return {
        "name": truncate(name.strip(), 100),
        "color": color.strip() or "#3b82f6",
        "kind": kind,
        "target_value": target_value,
        "target_period": period,
        "source_match": source_match.strip() if kind == "duration" else "",
    }


def _entry_display(kind: str, value: int) -> str:
    """Human rendering of one entry value ('45m', '+2', '✓', '7/10')."""
    if kind == "duration":
        return f"{value}m"
    if kind == "count":
        return f"+{value}"
    if kind == "boolean":
        return "✓" if value == 1 else "✗"
    return f"{value}/10"


def _metric_progress(metric: dict, entries: list[dict], exp_start: date, num_weeks: int, today: date) -> dict | None:
    """Progress vs the metric's own target; only count/boolean metrics have targets."""
    if metric["kind"] not in ("count", "boolean") or not metric["target_value"]:
        return None
    tv, period = metric["target_value"], metric["target_period"]
    _, exp_end = experiment_calendar(exp_start, num_weeks)
    week_start = today - timedelta(days=today.weekday())
    windows = {
        "total": (exp_start, exp_end),
        "week": (week_start, week_start + timedelta(days=6)),
        "day": (today, today),
    }
    lo, hi = windows[period]
    mine = [
        e for e in entries if e["activity_type_id"] == metric["id"] and lo.isoformat() <= e["date"] <= hi.isoformat()
    ]

    if metric["kind"] == "boolean" and period == "day":
        # "Every day": measure yes-days against elapsed experiment days.
        # Both window and denominator clamp to the experiment end, so stray
        # post-end entries (API accepts any in-window date) can't yield 15/14.
        window_end = min(today, exp_end)
        active_days = (exp_end - exp_start).days + 1
        elapsed = max(1, min((today - exp_start).days + 1, active_days))
        all_mine = [e for e in entries if e["activity_type_id"] == metric["id"] and e["value"] == 1]
        done = len({e["date"] for e in all_mine if exp_start.isoformat() <= e["date"] <= window_end.isoformat()})
        return {
            "name": metric["name"],
            "color": metric["color"],
            "label": f"{done}/{elapsed} days",
            "pct": min(100, round(done / elapsed * 100)),
            "met": done >= elapsed,
        }

    if metric["kind"] == "boolean":
        logged = len({e["date"] for e in mine if e["value"] == 1})
    else:
        logged = sum(e["value"] for e in mine)
    suffix = {"total": "", "week": " this week", "day": " today"}[period]
    unit = " days" if metric["kind"] == "boolean" else ""
    return {
        "name": metric["name"],
        "color": metric["color"],
        "label": f"{logged}/{tv}{unit}{suffix}",
        "pct": min(100, round(logged / tv * 100)),
        "met": logged >= tv,
    }


def _week_metric_lines(activity_types: list[dict], week_entries: list[dict]) -> list[dict]:
    """Per-week progress lines for non-duration metrics ('Bramka 5/8', 'Medytacja 6/7')."""
    lines = []
    for at in activity_types:
        mine = [e for e in week_entries if e["activity_type_id"] == at["id"]]
        if at["kind"] == "boolean":
            done = len({e["date"] for e in mine if e["value"] == 1})
            denom = at["target_value"] if (at["target_value"] and at["target_period"] == "week") else 7
            lines.append({"name": at["name"], "color": at["color"], "text": f"{done}/{denom}", "met": done >= denom})
        elif at["kind"] == "count" and at["target_value"] and at["target_period"] == "week":
            total = sum(e["value"] for e in mine)
            lines.append(
                {
                    "name": at["name"],
                    "color": at["color"],
                    "text": f"{total}/{at['target_value']}",
                    "met": total >= at["target_value"],
                }
            )
    return lines


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

    type_colors = {at["id"]: at["color"] for at in activity_types}
    duration_types = [at for at in activity_types if at["kind"] == "duration"]
    duration_ids = {at["id"] for at in duration_types}
    type_names = {at["id"]: at["name"] for at in activity_types}
    has_duration = bool(duration_types)
    # A single-boolean experiment reads best as a filled ✓ calendar.
    single_boolean = len(activity_types) == 1 and activity_types[0]["kind"] == "boolean"

    grid = []
    for wn in range(1, experiment["num_weeks"] + 1):
        monday = start_monday + timedelta(weeks=wn - 1)
        sunday = monday + timedelta(days=6)
        week_cfg = weeks_by_num.get(wn, {"label": "", "target_min": 0, "target_max": 0})

        days = []
        week_total = 0
        week_entries: list[dict] = []
        week_entries_by_type: dict[int, int] = {}
        for d in range(7):
            day_date = monday + timedelta(days=d)
            day_str = day_date.isoformat()
            day_entries = entries_by_date.get(day_str, [])
            week_entries.extend(day_entries)
            is_today = day_date == today

            duration_mins = sum(e["value"] for e in day_entries if e["activity_type_id"] in duration_ids)
            week_total += duration_mins

            # Track per-type minute totals for the "need X" status hints (duration only)
            for e in day_entries:
                if e["activity_type_id"] in duration_ids:
                    tid = e["activity_type_id"]
                    week_entries_by_type[tid] = week_entries_by_type.get(tid, 0) + e["value"]

            # Cell fill: dominated by duration; single-boolean experiments fill on ✓
            color = None
            label = ""
            if duration_mins:
                first_duration = next(e for e in day_entries if e["activity_type_id"] in duration_ids)
                color = type_colors.get(first_duration["activity_type_id"])
                label = f"{duration_mins}m"
            elif single_boolean and any(e["value"] == 1 for e in day_entries):
                color = activity_types[0]["color"]
                label = "✓"

            # Compact per-metric markers for non-duration metrics
            dots = []
            for at in activity_types:
                if at["kind"] == "duration" or (single_boolean and color):
                    continue
                es = [e for e in day_entries if e["activity_type_id"] == at["id"]]
                if not es:
                    continue
                if at["kind"] == "boolean":
                    text = "✓" if es[-1]["value"] == 1 else "✗"
                elif at["kind"] == "count":
                    text = str(sum(e["value"] for e in es))
                else:  # scale — day average
                    text = str(round(sum(e["value"] for e in es) / len(es)))
                dots.append({"color": at["color"], "text": text, "title": at["name"]})

            days.append(
                {
                    "date": day_str,
                    "weekday": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d],
                    "is_today": is_today,
                    "entries": day_entries,
                    "total_mins": duration_mins,
                    "color": color,
                    "label": label,
                    "dots": dots,
                }
            )

        # Progress calculation (weekly minutes window — duration metrics only)
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
        elif not has_duration:
            status = f"{len(week_entries)} logged"
            status_class = "active" if is_current else "muted"
        elif target_min <= 0:
            status = f"{week_total}m logged" if week_total else "no target"
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
            # Build smart status: which duration types are missing
            parts = []
            if remaining > 0:
                parts.append(f"{remaining}m left")
            for at in duration_types:
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
                "has_duration": has_duration,
                "metric_lines": _week_metric_lines(activity_types, week_entries),
                "per_type": {type_names.get(tid, "?"): mins for tid, mins in week_entries_by_type.items()},
            }
        )

    return grid


async def _current_job(db, job_id: int | None) -> dict | None:
    from app.routers.jobs import current_job_view

    return await current_job_view(db, job_id)


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

    minutes_by_exp: dict[int, int] = {}
    entries_by_exp: dict[int, int] = {}
    types_by_exp: dict[int, list[dict]] = {}
    targets_by_exp: dict[int, int] = {}

    if exp_ids:
        ph = ",".join("?" * len(exp_ids))

        # Duration minutes + raw entry counts per experiment
        total_rows = await db.execute_fetchall(
            f"SELECT ee.experiment_id, "
            f"COALESCE(SUM(CASE WHEN eat.kind = 'duration' THEN ee.value ELSE 0 END), 0) as minutes, "
            f"COUNT(*) as entries "
            f"FROM experiment_entries ee JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id "
            f"WHERE ee.experiment_id IN ({ph}) GROUP BY ee.experiment_id",
            exp_ids,
        )
        minutes_by_exp = {r["experiment_id"]: r["minutes"] for r in total_rows}
        entries_by_exp = {r["experiment_id"]: r["entries"] for r in total_rows}

        # Metrics per experiment
        type_rows = await db.execute_fetchall(
            f"SELECT experiment_id, name, color, kind FROM experiment_activity_types "
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
        _, end = experiment_calendar(start, exp["num_weeks"])
        exp["start_fmt"] = _fmt_date(start)
        exp["end_fmt"] = _fmt_date(end)
        exp["current_week"] = experiment_week_number(start, exp["num_weeks"], today)
        exp["weeks_done"] = 0 if today < start else exp["num_weeks"] if today > end else exp["current_week"] - 1
        # Day-based on purpose. weeks_done/num_weeks rendered "0% elapsed" for the
        # whole of week 1, right next to a "Week 1/4" label saying the opposite.
        # The label the template prints is "% elapsed", so it has to measure
        # elapsed time.
        total_days = (end - start).days + 1
        elapsed_days = min(max((today - start).days + 1, 0), total_days)
        exp["progress_pct"] = round(elapsed_days / total_days * 100) if total_days else 0
        exp["activity_types"] = types_by_exp.get(exp["id"], [])
        has_duration = any(at["kind"] == "duration" for at in exp["activity_types"])
        if has_duration:
            exp["logged_label"] = f"{minutes_by_exp.get(exp['id'], 0)}m"
            exp["target_label"] = f"{targets_by_exp.get(exp['id'], 0)}m"
        else:
            exp["logged_label"] = str(entries_by_exp.get(exp["id"], 0))
            exp["target_label"] = "—"

    return templates.TemplateResponse("experiments.html", {"request": request, "experiments": experiments})


@router.get("/new", response_class=HTMLResponse)
async def new_experiment_form(request: Request):
    db = get_user_db_from_request(request)
    goals = await db.execute_fetchall(
        "SELECT id, content FROM goals WHERE status = 'active' ORDER BY active DESC, updated_at DESC"
    )
    return templates.TemplateResponse(
        "experiment_new.html",
        {"request": request, "today": date.today().isoformat(), "goals": [dict(row) for row in goals]},
    )


@router.post("/create")
async def create_experiment(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    start_date: str = Form(...),
    num_weeks: int = Form(...),
    target_min: OptionalFormInt = None,  # blank when the weekly-targets card is hidden
    target_max: OptionalFormInt = None,
    metric_names: list[str] = Form(default=[]),  # noqa: B008
    metric_colors: list[str] = Form(default=[]),  # noqa: B008
    metric_kinds: list[str] = Form(default=[]),  # noqa: B008
    metric_targets: list[str] = Form(default=[]),  # noqa: B008
    metric_periods: list[str] = Form(default=[]),  # noqa: B008
    source_matches: list[str] = Form(default=[]),  # noqa: B008
    week_labels: str = Form(""),
    goal_id: OptionalFormInt = None,
):
    if not valid_date(start_date):
        return error_redirect(request, "/experiments/new", "Choose a valid experiment start date.")
    if num_weeks < 1 or num_weeks > 52:
        return error_redirect(request, "/experiments/new", "Experiment duration must be between 1 and 52 weeks.")
    title = truncate(title, 200)
    description = truncate(description, 2000)
    if not title.strip():
        return error_redirect(request, "/experiments/new", "Experiment title is required.")
    # Normalize targets the same way week editing does — an inverted range
    # otherwise renders nonsense progress percentages.
    target_min = max(0, target_min or 0)
    target_max = max(target_min, target_max or 0)

    # Parse metric rows up front — weekly minute targets only mean something
    # when at least one duration metric exists.
    def _get(lst: list[str], i: int) -> str:
        return lst[i] if i < len(lst) else ""

    metrics = []
    for i, name in enumerate(metric_names):
        metric = _normalize_metric(
            name,
            _get(metric_colors, i),
            _get(metric_kinds, i),
            _get(metric_targets, i),
            _get(metric_periods, i),
            _get(source_matches, i),
        )
        if metric is not None:
            metrics.append(metric)
    normalized_names = [metric["name"].casefold() for metric in metrics]
    if len(normalized_names) != len(set(normalized_names)):
        return error_redirect(request, "/experiments/new", "Metric names must be unique within an experiment.")

    if not any(m["kind"] == "duration" for m in metrics):
        target_min = target_max = 0
        week_labels = ""

    db = get_user_db_from_request(request)
    if goal_id is not None:
        goal = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (goal_id,))
        if not goal:
            return error_redirect(request, "/experiments/new", "Linked goal was not found.")

    cursor = await db.execute(
        "INSERT INTO experiments (goal_id, title, description, start_date, num_weeks) VALUES (?, ?, ?, ?, ?)",
        (goal_id, title.strip(), description, start_date, num_weeks),
    )
    exp_id = cursor.lastrowid

    for order, metric in enumerate(metrics, start=1):
        await db.execute(
            "INSERT INTO experiment_activity_types "
            "(experiment_id, name, color, kind, target_value, target_period, source_match, display_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                exp_id,
                metric["name"],
                metric["color"],
                metric["kind"],
                metric["target_value"],
                metric["target_period"],
                metric["source_match"],
                order,
            ),
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
    return success_redirect(request, f"/experiments/{exp_id}", "Experiment created.")


@router.get("/{experiment_id}", response_class=HTMLResponse)
async def experiment_detail(request: Request, experiment_id: int, job_id: int | None = Query(None, ge=1)):
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
    _, end = experiment_calendar(start, experiment["num_weeks"])
    today = date.today()

    kinds_by_id = {at["id"]: at["kind"] for at in activity_types}
    has_duration = any(at["kind"] == "duration" for at in activity_types)

    total_minutes = sum(e["value"] for e in entries if kinds_by_id.get(e["activity_type_id"]) == "duration")
    target_total = (
        sum((w.get("target_min", 0) + w.get("target_max", 0)) // 2 for w in weeks) if weeks and has_duration else 0
    )
    current_week = experiment_week_number(start, experiment["num_weeks"], today)
    elapsed_weeks = 0 if today < start else experiment["num_weeks"] if today > end else current_week - 1

    # Kind-aware per-metric stats
    type_stats = []
    for at in activity_types:
        es = [e for e in entries if e["activity_type_id"] == at["id"]]
        if at["kind"] == "duration":
            display = f"{sum(e['value'] for e in es)}m"
        elif at["kind"] == "count":
            display = f"{sum(e['value'] for e in es)}×"
        elif at["kind"] == "boolean":
            display = f"{len({e['date'] for e in es if e['value'] == 1})}d"
        else:  # scale
            display = f"~{sum(e['value'] for e in es) / len(es):.1f}" if es else "—"
        type_stats.append({"name": at["name"], "color": at["color"], "kind": at["kind"], "display": display})

    metric_progress = []
    for at in activity_types:
        progress = _metric_progress(at, entries, start, experiment["num_weeks"], today)
        if progress:
            metric_progress.append(progress)

    # Days left, not weeks done: "0 weeks done" through the whole of week 1 reads
    # as no progress, next to a "Week 1 of 4" label that says the opposite.
    total_days = (end - start).days + 1
    days_left = max(0, (end - max(today, start)).days + (1 if today < start else 0))

    stats = {
        "total_minutes": total_minutes,
        "target_total": target_total,
        "weeks_done": elapsed_weeks,
        "current_week": current_week,
        "total_days": total_days,
        "days_left": days_left,
        "type_stats": type_stats,
        "metric_progress": metric_progress,
        "has_duration": has_duration,
    }

    # Precompute kind-aware value rendering for the entries table
    for e in entries:
        e["display"] = _entry_display(kinds_by_id.get(e["activity_type_id"], "duration"), e["value"])

    # Check Oura connection
    oura_rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
    oura_connected = bool(oura_rows and oura_rows[0]["status"] == "connected")

    # LLM summaries. A GET renders what exists and nothing else: missing weeks
    # are queued by the scheduler, so opening this page can no longer spend money.
    from app.services.experiment_summary import get_existing_summaries, has_llm

    llm_available = await has_llm(db)
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
            "current_job": await _current_job(db, job_id),
        },
    )


@router.get("/{experiment_id}/edit", response_class=HTMLResponse)
async def edit_experiment_form(request: Request, experiment_id: int):
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not rows:
        return RedirectResponse("/experiments", status_code=303)
    experiment = dict(rows[0])

    metrics = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT at.*, (SELECT COUNT(*) FROM experiment_entries ee WHERE ee.activity_type_id = at.id) AS entry_count "
            "FROM experiment_activity_types at WHERE at.experiment_id = ? ORDER BY at.display_order",
            (experiment_id,),
        )
    ]
    goals = [
        dict(row)
        for row in await db.execute_fetchall(
            "SELECT id, content, status FROM goals WHERE status IN ('active', 'paused') OR id = ? "
            "ORDER BY active DESC, updated_at DESC",
            (experiment["goal_id"] or -1,),
        )
    ]

    return templates.TemplateResponse(
        "experiment_edit.html",
        {"request": request, "experiment": experiment, "metrics": metrics, "goals": goals},
    )


@router.post("/{experiment_id}/edit")
async def edit_experiment(
    request: Request,
    experiment_id: int,
    title: str = Form(...),
    description: str = Form(""),
    start_date: str = Form(...),
    num_weeks: int = Form(...),
    status: str = Form("active"),
    goal_id: OptionalFormInt = None,
):
    if not valid_date(start_date):
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Choose a valid experiment start date.")
    if num_weeks < 1 or num_weeks > 52:
        return error_redirect(
            request,
            f"/experiments/{experiment_id}/edit",
            "Experiment duration must be between 1 and 52 weeks.",
        )
    title = truncate(title, 200).strip()
    if not title:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Experiment title is required.")
    if status not in ("active", "completed", "abandoned"):
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Choose a valid experiment status.")

    db = get_user_db_from_request(request)
    # Unknown id: the week INSERT below would otherwise raise an FK violation (500).
    rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not rows:
        return error_redirect(request, "/experiments", "Experiment not found.")
    if goal_id is not None:
        goal = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (goal_id,))
        if not goal:
            return error_redirect(request, f"/experiments/{experiment_id}/edit", "Linked goal was not found.")
    await db.execute(
        "UPDATE experiments SET goal_id = ?, title = ?, description = ?, start_date = ?, num_weeks = ?, status = ? WHERE id = ?",
        (goal_id, title, truncate(description, 2000), start_date, num_weeks, status, experiment_id),
    )

    # Resync weeks: drop rows beyond the new horizon, add missing ones.
    # New weeks inherit the last surviving week's targets (a sane template).
    await db.execute(
        "DELETE FROM experiment_weeks WHERE experiment_id = ? AND week_number > ?", (experiment_id, num_weeks)
    )
    last = await db.execute_fetchall(
        "SELECT target_min, target_max FROM experiment_weeks WHERE experiment_id = ? ORDER BY week_number DESC LIMIT 1",
        (experiment_id,),
    )
    tmin, tmax = (last[0]["target_min"], last[0]["target_max"]) if last else (0, 0)
    for wn in range(1, num_weeks + 1):
        await db.execute(
            "INSERT OR IGNORE INTO experiment_weeks (experiment_id, week_number, label, target_min, target_max) "
            "VALUES (?, ?, '', ?, ?)",
            (experiment_id, wn, tmin, tmax),
        )

    await db.commit()
    return success_redirect(request, f"/experiments/{experiment_id}", "Experiment updated.")


@router.post("/{experiment_id}/metric/add")
async def add_metric(
    request: Request,
    experiment_id: int,
    name: str = Form(...),
    color: str = Form("#3b82f6"),
    kind: str = Form("duration"),
    target_value: str = Form("0"),
    target_period: str = Form("week"),
    source_match: str = Form(""),
):
    metric = _normalize_metric(name, color, kind, target_value, target_period, source_match)
    if metric is None:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Metric name is required.")
    db = get_user_db_from_request(request)
    # Unknown id: the metric INSERT below would otherwise raise an FK violation (500).
    rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not rows:
        return error_redirect(request, "/experiments", "Experiment not found.")
    existing = await db.execute_fetchall(
        "SELECT name FROM experiment_activity_types WHERE experiment_id = ?", (experiment_id,)
    )
    if any(row["name"].casefold() == metric["name"].casefold() for row in existing):
        return error_redirect(
            request,
            f"/experiments/{experiment_id}/edit",
            "Metric names must be unique within an experiment.",
        )
    order_rows = await db.execute_fetchall(
        "SELECT COALESCE(MAX(display_order), 0) AS mx FROM experiment_activity_types WHERE experiment_id = ?",
        (experiment_id,),
    )
    await db.execute(
        "INSERT INTO experiment_activity_types "
        "(experiment_id, name, color, kind, target_value, target_period, source_match, display_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            experiment_id,
            metric["name"],
            metric["color"],
            metric["kind"],
            metric["target_value"],
            metric["target_period"],
            metric["source_match"],
            order_rows[0]["mx"] + 1,
        ),
    )
    await db.commit()
    return success_redirect(request, f"/experiments/{experiment_id}/edit", "Metric added.")


@router.post("/{experiment_id}/metric/{metric_id}/update")
async def update_metric(
    request: Request,
    experiment_id: int,
    metric_id: int,
    name: str = Form(...),
    color: str = Form("#3b82f6"),
    target_value: str = Form("0"),
    target_period: str = Form("week"),
    source_match: str = Form(""),
):
    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Experiment not found.")
    rows = await db.execute_fetchall(
        "SELECT kind FROM experiment_activity_types WHERE id = ? AND experiment_id = ?", (metric_id, experiment_id)
    )
    if not rows:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Metric not found.")
    # kind is immutable — changing it would silently reinterpret logged values.
    metric = _normalize_metric(name, color, rows[0]["kind"], target_value, target_period, source_match)
    if metric is None:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Metric name is required.")
    siblings = await db.execute_fetchall(
        "SELECT name FROM experiment_activity_types WHERE experiment_id = ? AND id != ?",
        (experiment_id, metric_id),
    )
    if any(row["name"].casefold() == metric["name"].casefold() for row in siblings):
        return error_redirect(
            request,
            f"/experiments/{experiment_id}/edit",
            "Metric names must be unique within an experiment.",
        )
    cursor = await db.execute(
        "UPDATE experiment_activity_types SET name = ?, color = ?, target_value = ?, target_period = ?, source_match = ? "
        "WHERE id = ? AND experiment_id = ?",
        (
            metric["name"],
            metric["color"],
            metric["target_value"],
            metric["target_period"],
            metric["source_match"],
            metric_id,
            experiment_id,
        ),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Metric not found.")
    return success_redirect(request, f"/experiments/{experiment_id}/edit", "Metric updated.")


@router.post("/{experiment_id}/metric/{metric_id}/delete")
async def delete_metric(request: Request, experiment_id: int, metric_id: int):
    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Experiment not found.")
    # Entries cascade via FK (activity_type_id ... ON DELETE CASCADE).
    cursor = await db.execute(
        "DELETE FROM experiment_activity_types WHERE id = ? AND experiment_id = ?", (metric_id, experiment_id)
    )
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, f"/experiments/{experiment_id}/edit", "Metric not found.")
    return success_redirect(request, f"/experiments/{experiment_id}/edit", "Metric deleted.")


@router.post("/{experiment_id}/entry")
async def add_entry(
    request: Request,
    experiment_id: int,
    date: str = Form(...),
    metric_id: int = Form(...),
    value: str = Form("1"),
    notes: str = Form(""),
):
    from datetime import date as calendar_date

    if not valid_date(date):
        return error_redirect(request, f"/experiments/{experiment_id}", "Choose a valid entry date.")
    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    experiment = dict(experiment_rows[0])
    if experiment["status"] != "active":
        return error_redirect(request, f"/experiments/{experiment_id}", "Only active experiments accept entries.")
    experiment_start = calendar_date.fromisoformat(experiment["start_date"])
    _, experiment_end = experiment_calendar(experiment_start, experiment["num_weeks"])
    if not (experiment_start.isoformat() <= date <= experiment_end.isoformat()):
        return error_redirect(
            request,
            f"/experiments/{experiment_id}",
            f"Entry date must be between {experiment_start} and {experiment_end}.",
        )
    rows = await db.execute_fetchall(
        "SELECT kind FROM experiment_activity_types WHERE id = ? AND experiment_id = ?", (metric_id, experiment_id)
    )
    if not rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Metric not found.")
    kind = rows[0]["kind"]
    try:
        v = int(value)
    except (TypeError, ValueError):
        return error_redirect(request, f"/experiments/{experiment_id}", "Entry value must be a whole number.")
    v = clamp_metric_value(kind, v)
    if v is None:
        return error_redirect(request, f"/experiments/{experiment_id}", "Entry value is outside the allowed range.")
    notes = truncate(notes, 500)

    if kind == "boolean":
        # One row per metric per day — the latest answer wins.
        await db.execute(
            "DELETE FROM experiment_entries WHERE experiment_id = ? AND activity_type_id = ? AND date = ?",
            (experiment_id, metric_id, date),
        )
    await db.execute(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, value, notes) VALUES (?, ?, ?, ?, ?)",
        (experiment_id, date, metric_id, v, notes),
    )
    await db.commit()
    return success_redirect(request, f"/experiments/{experiment_id}", "Entry logged.")


@router.post("/{experiment_id}/generate-summary")
async def generate_summary(
    request: Request,
    experiment_id: int,
    week_number: int = Form(...),
):
    """Queue the summary. The provider call belongs to the worker."""
    from app.services.experiment_summary import SUMMARY_JOB_KIND, has_llm
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key

    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    week_rows = await db.execute_fetchall(
        "SELECT 1 FROM experiment_weeks WHERE experiment_id = ? AND week_number = ?",
        (experiment_id, week_number),
    )
    if not week_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment week not found.")
    if not await has_llm(db):
        return error_redirect(
            request, f"/experiments/{experiment_id}", "No AI provider is configured. Add one in Settings."
        )
    try:
        result = await enqueue_paid_llm_job(
            db,
            SUMMARY_JOB_KIND,
            {"experiment_id": experiment_id, "week_number": week_number, "trigger": "manual"},
            idempotency_key=paid_llm_job_key(SUMMARY_JOB_KIND, str(experiment_id), str(week_number), "manual"),
        )
    except ActiveWorkloadConflictError:
        return error_redirect(request, f"/experiments/{experiment_id}", "Another week summary is already queued.")
    except Exception:
        logger.exception("Failed to enqueue summary for experiment %s week %s", experiment_id, week_number)
        return error_redirect(
            request, f"/experiments/{experiment_id}", "The week summary could not be queued. Try again."
        )
    from app.routers.jobs import wake_worker_for

    wake_worker_for(request)

    return success_redirect(request, f"/experiments/{experiment_id}?job_id={result.job_id}", "Week summary queued.")


@router.post("/{experiment_id}/import-workouts")
async def import_workouts(request: Request, experiment_id: int):
    from app.services.oura_api import _auto_populate_experiments

    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    try:
        await _auto_populate_experiments(db)
        await db.commit()
    except Exception:
        logger.exception("Failed to import Oura workouts for experiment %s", experiment_id)
        return error_redirect(request, f"/experiments/{experiment_id}", "Could not import Oura workouts. Try again.")
    return success_redirect(request, f"/experiments/{experiment_id}", "Oura workout import completed.")


@router.post("/{experiment_id}/delete-entry")
async def delete_entry(
    request: Request,
    experiment_id: int,
    entry_id: int = Form(...),
):
    db = get_user_db_from_request(request)
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    cursor = await db.execute(
        "DELETE FROM experiment_entries WHERE id = ? AND experiment_id = ?", (entry_id, experiment_id)
    )
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, f"/experiments/{experiment_id}", "Entry not found.")
    return success_redirect(request, f"/experiments/{experiment_id}", "Entry deleted.")


@router.post("/{experiment_id}/complete")
async def complete_experiment(
    request: Request,
    experiment_id: int,
    new_status: str = Form("completed"),
):
    db = get_user_db_from_request(request)
    # 'active' allows undoing a mistaken Complete/Abandon click (reopen).
    messages = {
        "completed": "Experiment marked as completed.",
        "abandoned": "Experiment marked as abandoned.",
        "active": "Experiment reopened.",
    }
    if new_status not in messages:
        return error_redirect(request, f"/experiments/{experiment_id}", "Choose a valid experiment status.")
    cursor = await db.execute("UPDATE experiments SET status = ? WHERE id = ?", (new_status, experiment_id))
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    return success_redirect(request, f"/experiments/{experiment_id}", messages[new_status])


@router.post("/{experiment_id}/delete")
async def delete_experiment(request: Request, experiment_id: int):
    db = get_user_db_from_request(request)
    cursor = await db.execute("DELETE FROM experiments WHERE id = ?", (experiment_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, "/experiments", "Experiment not found.")
    return success_redirect(request, "/experiments", "Experiment deleted.")


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
    experiment_rows = await db.execute_fetchall("SELECT id FROM experiments WHERE id = ?", (experiment_id,))
    if not experiment_rows:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment not found.")
    cursor = await db.execute(
        "UPDATE experiment_weeks SET target_min = ?, target_max = ?, label = ? "
        "WHERE experiment_id = ? AND week_number = ?",
        (target_min, target_max, label.strip(), experiment_id, week_number),
    )
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, f"/experiments/{experiment_id}", "Experiment week not found.")
    return success_redirect(request, f"/experiments/{experiment_id}", f"Week {week_number} targets updated.")
