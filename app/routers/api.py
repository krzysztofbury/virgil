"""REST API for machine-to-machine access (OpenClaw, AI agents, scripts).

Auth: `X-API-Key` header, compared in constant time against VIRGIL_API_KEY.
The key maps to a single user's database: VIRGIL_API_USER_EMAIL if set,
otherwise the first active admin account. API is disabled when VIRGIL_API_KEY is empty.
Writes cover canonical goals/reps, experiment logging, and exercise-library CRUD.
"""

import hmac
from datetime import date, timedelta
from typing import Annotated
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from app.central_db import get_central_db
from app.config import API_KEY, API_USER_EMAIL
from app.library_validation import LibraryWriteError, normalize_tag, normalize_tags, validate_library_write
from app.services.experiment_weeks import experiment_calendar, experiment_week_number
from app.services.goal_data import (
    GOAL_STATUSES,
    REP_STATUSES,
    GoalDataError,
    create_goal,
    create_rep,
    delete_goal_record,
    delete_rep_record,
    transition_rep,
    update_goal,
    update_rep,
)
from app.services.streak import get_streak, get_week_clean
from app.user_db import close_user_db, open_user_db
from app.validation import clamp_metric_value, truncate, valid_date

router = APIRouter(prefix="/api", tags=["api"])

HABIT_FIELDS = (
    "morning_routine",
    "evening_routine",
    "water",
    "andy_body_status",
    "andy_spirit_status",
    "andy_account_status",
    "andy_relations_status",
)


async def api_db(request: Request):
    """Authenticate via X-API-Key and yield the mapped user's DB connection."""
    if not API_KEY:
        raise HTTPException(status_code=403, detail="API disabled (VIRGIL_API_KEY not set)")
    provided = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(provided.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid API key")

    central = await get_central_db()
    if API_USER_EMAIL:
        rows = await central.execute_fetchall(
            "SELECT db_filename FROM users WHERE email = ? AND is_active = 1",
            (API_USER_EMAIL.lower(),),
        )
    else:
        rows = await central.execute_fetchall(
            "SELECT db_filename FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY created_at LIMIT 1"
        )
    if not rows:
        raise HTTPException(status_code=503, detail="API user not found")

    db = await open_user_db(rows[0]["db_filename"])
    try:
        yield db
    finally:
        await close_user_db(db)


ApiDb = Annotated[aiosqlite.Connection, Depends(api_db)]


@router.get("/summary")
async def api_summary(db: ApiDb):
    """Today's snapshot: daily habits, Feniks streak, latest Oura, training this week, latest measurements."""
    today = date.today()
    today_iso = today.isoformat()

    log_rows = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (today_iso,))
    daily = None
    if log_rows:
        log = dict(log_rows[0])
        daily = {
            "energy": log["energy"],
            "habits": {f: log[f] for f in HABIT_FIELDS},
            "notes": log["notes"],
        }

    streak_days, last_relapse = await get_streak(db)

    oura_rows = await db.execute_fetchall("SELECT * FROM oura_daily ORDER BY date DESC LIMIT 1")

    week_start = (today - timedelta(days=today.weekday())).isoformat()
    sess = await db.execute_fetchall(
        "SELECT COUNT(*) AS n, MAX(date) AS last_date FROM training_sessions WHERE date >= ?",
        (week_start,),
    )

    meas_rows = await db.execute_fetchall("SELECT * FROM body_measurements ORDER BY date DESC LIMIT 1")

    return {
        "date": today_iso,
        "daily": daily,
        "feniks": {
            "streak_days": streak_days,
            "last_relapse": last_relapse.isoformat() if last_relapse else None,
        },
        "oura_latest": dict(oura_rows[0]) if oura_rows else None,
        "training_week": {"sessions": sess[0]["n"], "last_date": sess[0]["last_date"]},
        "measurements_latest": dict(meas_rows[0]) if meas_rows else None,
    }


@router.get("/oura/today")
async def api_oura_today(db: ApiDb):
    """Latest synced Oura vitals (may lag today by one sync interval)."""
    rows = await db.execute_fetchall("SELECT * FROM oura_daily ORDER BY date DESC LIMIT 1")
    if not rows:
        raise HTTPException(status_code=404, detail="No Oura data")
    return dict(rows[0])


@router.get("/habits")
async def api_habits(
    db: ApiDb,
    days: int = Query(7, ge=1, le=90, alias="range"),
):
    """Habit completion for the last N days (?range=7)."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = await db.execute_fetchall(
        "SELECT date, energy, morning_routine, evening_routine, water, "
        "andy_body_status, andy_spirit_status, andy_account_status, andy_relations_status "
        "FROM daily_logs WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    return {"range_days": days, "since": since, "logs": [dict(r) for r in rows]}


def _metric_logged(kind: str, entries: list[dict], lo: str, hi: str) -> int | float:
    """Aggregate entry values for one metric inside [lo, hi] (ISO dates), by kind:
    boolean → distinct yes-days, scale → average, duration/count → sum."""
    sel = [e for e in entries if lo <= e["date"] <= hi]
    if kind == "boolean":
        return len({e["date"] for e in sel if e["value"] == 1})
    if kind == "scale":
        return round(sum(e["value"] for e in sel) / len(sel), 1) if sel else 0
    return sum(e["value"] for e in sel)


class ApiGoalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: int
    horizon: str
    content: str
    status: str = "active"
    start_date: str | None = None
    end_date: str | None = None
    focus: bool = False
    idempotency_key: str = ""
    parent_goal_id: int | None = None


class ApiGoalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area_id: int | None = None
    horizon: str | None = None
    content: str | None = None
    status: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    focus: bool | None = None
    display_order: int | None = None
    parent_goal_id: int | None = None


class ApiRepCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: int
    content: str
    period: str
    due_date: str
    notes: str = ""
    idempotency_key: str = ""


class ApiRepPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: int | None = None
    content: str | None = None
    period: str | None = None
    due_date: str | None = None
    notes: str | None = None


class ApiRepTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    due_date: str | None = None
    period: str | None = None


def _goal_error(exc: GoalDataError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


async def _goals_with_context(db, where: str = "", params: tuple = (), limit: int = 100) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT g.*, a.name AS area_name, a.icon AS area_icon, "
        "(SELECT COUNT(*) FROM goal_reps r WHERE r.goal_id = g.id AND r.status = 'pending') AS pending_reps "
        "FROM goals g JOIN goal_areas a ON a.id = g.area_id "
        f"{where} ORDER BY g.active DESC, g.updated_at DESC, g.id DESC LIMIT ?",
        (*params, limit),
    )
    goals = [dict(row) for row in rows]
    if not goals:
        return goals
    ids = [goal["id"] for goal in goals]
    placeholders = ",".join("?" * len(ids))
    experiments = await db.execute_fetchall(
        f"SELECT id, goal_id, title, status, start_date, num_weeks FROM experiments WHERE goal_id IN ({placeholders}) "
        "ORDER BY status, start_date, id",
        ids,
    )
    by_goal: dict[int, list[dict]] = {}
    for experiment in experiments:
        by_goal.setdefault(experiment["goal_id"], []).append(dict(experiment))
    for goal in goals:
        goal["experiments"] = by_goal.get(goal["id"], [])
    return goals


@router.get("/goal-areas")
async def api_goal_areas(db: ApiDb):
    rows = await db.execute_fetchall("SELECT id, name, icon, display_order FROM goal_areas ORDER BY display_order, id")
    return {"areas": [dict(row) for row in rows]}


@router.get("/goals")
async def api_goals(
    db: ApiDb,
    status: str | None = Query(None),
    focus: bool | None = Query(None),
    limit: int = Query(100, ge=1, le=200),
):
    """Goals with explicit lifecycle, date windows, pending-rep counts and linked experiments."""
    clauses: list[str] = []
    params: list = []
    if status is not None:
        if status not in GOAL_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(GOAL_STATUSES)}")
        clauses.append("g.status = ?")
        params.append(status)
    if focus is not None:
        clauses.append("g.active = ?")
        params.append(1 if focus else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    goals = await _goals_with_context(db, where, tuple(params), limit)
    return {"goals": goals, "limit": limit}


@router.get("/goals/{goal_id}")
async def api_goal(goal_id: int, db: ApiDb):
    goals = await _goals_with_context(db, "WHERE g.id = ?", (goal_id,), 1)
    if not goals:
        raise HTTPException(status_code=404, detail="Goal not found")
    return goals[0]


@router.post("/goals")
async def api_create_goal(payload: ApiGoalCreate, db: ApiDb):
    try:
        goal, created = await create_goal(
            db,
            area_id=payload.area_id,
            horizon=payload.horizon,
            content=payload.content,
            status=payload.status,
            start_date=payload.start_date,
            end_date=payload.end_date,
            active=payload.focus,
            source="api",
            source_ref=payload.idempotency_key or str(uuid4()),
            parent_goal_id=payload.parent_goal_id,
        )
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    goal["created"] = created
    return goal


@router.patch("/goals/{goal_id}")
async def api_update_goal(goal_id: int, payload: ApiGoalPatch, db: ApiDb):
    changes = payload.model_dump(exclude_unset=True)
    if "focus" in changes:
        changes["active"] = changes.pop("focus")
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one goal field")
    try:
        goal = await update_goal(db, goal_id, changes)
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    return goal


@router.delete("/goals/{goal_id}")
async def api_delete_goal(goal_id: int, db: ApiDb):
    try:
        await delete_goal_record(db, goal_id)
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    return {"deleted": True, "goal_id": goal_id}


async def _rep_by_id(db, rep_id: int) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT r.*, g.content AS goal_content, "
        "(SELECT child.id FROM goal_reps child WHERE child.carried_from_id = r.id) AS carried_to_id "
        "FROM goal_reps r JOIN goals g ON g.id = r.goal_id WHERE r.id = ?",
        (rep_id,),
    )
    return dict(rows[0]) if rows else None


@router.get("/goal-reps")
async def api_goal_reps(
    db: ApiDb,
    status: str | None = Query(None),
    goal_id: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Bounded execution-rep history. Use status=pending for the current queue."""
    clauses: list[str] = []
    params: list = []
    if status is not None:
        if status not in REP_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(REP_STATUSES)}")
        clauses.append("r.status = ?")
        params.append(status)
    if goal_id is not None:
        clauses.append("r.goal_id = ?")
        params.append(goal_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await db.execute_fetchall(
        "SELECT r.*, g.content AS goal_content, "
        "(SELECT child.id FROM goal_reps child WHERE child.carried_from_id = r.id) AS carried_to_id "
        "FROM goal_reps r JOIN goals g ON g.id = r.goal_id "
        f"{where} ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.due_date, r.updated_at DESC, r.id DESC LIMIT ?",
        (*params, limit),
    )
    return {"reps": [dict(row) for row in rows], "limit": limit}


@router.get("/goal-reps/{rep_id}")
async def api_goal_rep(rep_id: int, db: ApiDb):
    rep = await _rep_by_id(db, rep_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="Goal rep not found")
    return rep


@router.post("/goal-reps")
async def api_create_goal_rep(payload: ApiRepCreate, db: ApiDb):
    try:
        rep, created = await create_rep(
            db,
            goal_id=payload.goal_id,
            content=payload.content,
            period=payload.period,
            due_date=payload.due_date,
            notes=payload.notes,
            source="api",
            source_ref=payload.idempotency_key or str(uuid4()),
        )
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    rep["created"] = created
    return rep


@router.patch("/goal-reps/{rep_id}")
async def api_update_goal_rep(rep_id: int, payload: ApiRepPatch, db: ApiDb):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one rep field")
    try:
        rep = await update_rep(db, rep_id, changes)
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    return rep


@router.post("/goal-reps/{rep_id}/transition")
async def api_transition_goal_rep(rep_id: int, payload: ApiRepTransition, db: ApiDb):
    try:
        rep, carried_to = await transition_rep(
            db,
            rep_id,
            payload.action,
            due_date=payload.due_date,
            period=payload.period,
        )
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    return {"rep": rep, "carried_to": carried_to}


@router.delete("/goal-reps/{rep_id}")
async def api_delete_goal_rep(rep_id: int, db: ApiDb):
    try:
        await delete_rep_record(db, rep_id)
    except GoalDataError as exc:
        await db.rollback()
        raise _goal_error(exc) from exc
    await db.commit()
    return {"deleted": True, "rep_id": rep_id}


@router.get("/experiments/active")
async def api_experiments_active(db: ApiDb):
    """Active experiments: current-week minutes vs target plus per-metric progress
    (kind, target, logged today/this week/total)."""
    today = date.today()
    exps = await db.execute_fetchall("SELECT * FROM experiments WHERE status = 'active' ORDER BY start_date")
    result = []
    for row in exps:
        exp = dict(row)
        start = date.fromisoformat(exp["start_date"])
        start_monday, end = experiment_calendar(start, exp["num_weeks"])
        week_no = experiment_week_number(start, exp["num_weeks"], today)
        week_start = start_monday + timedelta(weeks=week_no - 1)
        week_end = week_start + timedelta(days=6)
        logged_start = max(week_start, start)
        logged_end = min(week_end, end)

        target_rows = await db.execute_fetchall(
            "SELECT label, target_min, target_max FROM experiment_weeks WHERE experiment_id = ? AND week_number = ?",
            (exp["id"], week_no),
        )
        logged = await db.execute_fetchall(
            "SELECT COALESCE(SUM(CASE WHEN eat.kind = 'duration' THEN ee.value ELSE 0 END), 0) AS total, "
            "COUNT(*) AS entries "
            "FROM experiment_entries ee JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id "
            "WHERE ee.experiment_id = ? AND ee.date BETWEEN ? AND ?",
            (exp["id"], logged_start.isoformat(), logged_end.isoformat()),
        )

        metric_rows = await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types WHERE experiment_id = ? ORDER BY display_order",
            (exp["id"],),
        )
        entry_rows = [
            dict(r)
            for r in await db.execute_fetchall(
                "SELECT date, activity_type_id, value FROM experiment_entries WHERE experiment_id = ?",
                (exp["id"],),
            )
        ]
        # All agent-facing weekly progress uses the same Monday-Sunday window.
        cal_week_start = logged_start
        cal_week_end = logged_end
        metrics = []
        for mr in metric_rows:
            m = dict(mr)
            mine = [e for e in entry_rows if e["activity_type_id"] == m["id"]]
            today_s = today.isoformat()
            metrics.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "kind": m["kind"],
                    "color": m["color"],
                    "target_value": m["target_value"],
                    "target_period": m["target_period"],
                    "logged_today": _metric_logged(m["kind"], mine, today_s, today_s),
                    "logged_week": _metric_logged(
                        m["kind"], mine, cal_week_start.isoformat(), cal_week_end.isoformat()
                    ),
                    "logged_total": _metric_logged(m["kind"], mine, start.isoformat(), end.isoformat()),
                }
            )

        result.append(
            {
                "id": exp["id"],
                "goal_id": exp["goal_id"],
                "title": exp["title"],
                "start_date": exp["start_date"],
                "week": week_no,
                "num_weeks": exp["num_weeks"],
                "week_window": {"from": week_start.isoformat(), "to": week_end.isoformat()},
                "week_target": dict(target_rows[0]) if target_rows else None,
                "week_logged": dict(logged[0]),
                "metrics": metrics,
            }
        )
    return {"experiments": result}


class ApiEntryIn(BaseModel):
    """Body of POST /experiments/{id}/entries. `metric` is a metric name or id;
    `value` semantics follow the metric kind: duration=minutes, count=events,
    boolean=1/0 (one per day, last write wins), scale=0-10."""

    metric: str | int
    value: int = 1
    date: str | None = None
    notes: str = ""


@router.post("/experiments/{experiment_id}/entries")
async def api_log_entry(experiment_id: int, payload: ApiEntryIn, db: ApiDb):
    """Log one entry into an active experiment (the API's only write)."""
    exp_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not exp_rows:
        raise HTTPException(status_code=404, detail="Experiment not found")
    if exp_rows[0]["status"] != "active":
        raise HTTPException(status_code=409, detail="Experiment is not active")

    metric_rows = await db.execute_fetchall(
        "SELECT * FROM experiment_activity_types WHERE experiment_id = ? ORDER BY display_order",
        (experiment_id,),
    )
    if isinstance(payload.metric, int) or (isinstance(payload.metric, str) and payload.metric.isdigit()):
        wanted_id = int(payload.metric)
        matches = [dict(m) for m in metric_rows if m["id"] == wanted_id]
    else:
        # Python casefold, not SQL LOWER(): SQLite lowercases ASCII only, which
        # breaks case-insensitive matching for Polish metric names ("Medytacja").
        wanted = payload.metric.strip().casefold()
        matches = [dict(m) for m in metric_rows if m["name"].casefold() == wanted]
    if not matches:
        raise HTTPException(status_code=404, detail="Metric not found in this experiment")
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="Metric name is ambiguous; use its numeric id")
    metric = matches[0]  # deterministic: first by display_order

    entry_date = payload.date or date.today().isoformat()
    if not valid_date(entry_date):
        raise HTTPException(status_code=422, detail="Invalid date (expected YYYY-MM-DD)")
    # Reject out-of-window dates: the entry would be invisible in the grid and
    # all progress windows — a silent success an MCP client can't notice.
    exp = dict(exp_rows[0])
    exp_start = date.fromisoformat(exp["start_date"])
    _, exp_end = experiment_calendar(exp_start, exp["num_weeks"])
    if not (exp_start.isoformat() <= entry_date <= exp_end.isoformat()):
        raise HTTPException(
            status_code=422,
            detail=f"Date {entry_date} outside the experiment window ({exp_start} – {exp_end})",
        )
    value = clamp_metric_value(metric["kind"], payload.value)
    if value is None:
        raise HTTPException(
            status_code=422,
            detail=f"Value {payload.value} out of bounds for kind '{metric['kind']}'",
        )

    if metric["kind"] == "boolean":
        # One row per metric per day — the latest answer wins.
        await db.execute(
            "DELETE FROM experiment_entries WHERE experiment_id = ? AND activity_type_id = ? AND date = ?",
            (experiment_id, metric["id"], entry_date),
        )
    cursor = await db.execute(
        "INSERT INTO experiment_entries (experiment_id, date, activity_type_id, value, notes, source, source_ref) "
        "VALUES (?, ?, ?, ?, ?, 'api', ?)",
        (experiment_id, entry_date, metric["id"], value, truncate(payload.notes, 500), str(uuid4())),
    )
    await db.commit()
    return {
        "ok": True,
        "entry_id": cursor.lastrowid,
        "experiment_id": experiment_id,
        "metric_id": metric["id"],
        "kind": metric["kind"],
        "date": entry_date,
        "value": value,
    }


@router.get("/training")
async def api_training(
    db: ApiDb,
    days: int = Query(7, ge=1, le=90, alias="range"),
):
    """Training sessions in the last N days with entry counts and core volume."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = await db.execute_fetchall(
        "SELECT s.id, s.date, s.duration_minutes, s.notes, COUNT(e.id) AS entries, "
        "COALESCE(SUM(CASE WHEN ex.metric = 'reps' THEN e.reps * COALESCE(e.weight, 0) ELSE 0 END), 0) AS volume_kg "
        "FROM training_sessions s "
        "LEFT JOIN training_entries e ON e.session_id = s.id "
        "LEFT JOIN training_exercises ex ON e.exercise_id = ex.id "
        "WHERE s.date >= ? GROUP BY s.id ORDER BY s.date DESC",
        (since,),
    )
    return {"range_days": days, "since": since, "sessions": [dict(r) for r in rows]}


@router.get("/training/detail")
async def api_training_detail(
    db: ApiDb,
    days: int = Query(7, ge=1, le=90, alias="range"),
):
    """Full per-set training detail for the last N days (?range=7): each session broken
    into exercises (grouped) and every set — reps+weight, or weight+seconds for timed
    lifts (carries/holds, metric='time')."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    sessions = await db.execute_fetchall(
        "SELECT id, date, duration_minutes, notes FROM training_sessions WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    # One batched entries query instead of one per session (a 90-day range
    # would otherwise fire dozens of serialized SQLite queries).
    entries_by_session: dict[int, list] = {}
    if sessions:
        session_ids = [s["id"] for s in sessions]
        placeholders = ",".join("?" * len(session_ids))
        all_entries = await db.execute_fetchall(
            f"SELECT e.session_id, ex.id AS exercise_id, ex.name, ex.section, ex.metric, "
            f"e.set_number, e.reps, e.weight, e.duration, e.notes "
            f"FROM training_entries e JOIN training_exercises ex ON e.exercise_id = ex.id "
            f"WHERE e.session_id IN ({placeholders}) ORDER BY ex.display_order, ex.name, e.set_number",
            session_ids,
        )
        for r in all_entries:
            entries_by_session.setdefault(r["session_id"], []).append(r)

    result = []
    for s in sessions:
        sess = dict(s)
        # Group by exercise ID, not name — two exercises may share a name and
        # must not have their sets merged.
        exercises: dict = {}
        order: list = []
        for r in entries_by_session.get(sess["id"], []):
            ex_id = r["exercise_id"]
            if ex_id not in exercises:
                exercises[ex_id] = {
                    "id": ex_id,
                    "name": r["name"],
                    "section": r["section"],
                    "metric": r["metric"],
                    "sets": [],
                }
                order.append(ex_id)
            exercises[ex_id]["sets"].append(
                {
                    "set": r["set_number"],
                    "reps": r["reps"],
                    "weight": r["weight"],
                    "duration": r["duration"],
                    # The metcon result the parser puts on the first entry of a
                    # movement: a finishing time, or rounds plus reps. Nothing
                    # read it, so an LLM asked "how did the WOD go" saw the
                    # sets and never the score.
                    "notes": r["notes"] or "",
                }
            )
        sess["exercises"] = [exercises[i] for i in order]
        result.append(sess)
    return {"range_days": days, "since": since, "sessions": result}


@router.get("/noporn")
async def api_noporn(
    db: ApiDb,
    days: int = Query(30, ge=1, le=365, alias="range"),
):
    """No-porn (Feniks) detail: config, streak, current-week clean rate (Gola), plus the
    relapse/reset events, journal entries (emotions/triggers/thoughts/coping) and logged
    pleasures from the last N days (?range=30). This is the WHY behind the streak.

    Gated behind VIRGIL_API_SENSITIVE — this is intimate journal content, and a
    leaked API key must not expose it by default."""
    from app import config

    if not config.API_SENSITIVE:
        raise HTTPException(
            status_code=403,
            detail="Sensitive scope disabled (set VIRGIL_API_SENSITIVE=true to expose /api/noporn)",
        )
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()

    conf = await db.execute_fetchall("SELECT start_date, target_days, big_why FROM feniks_config WHERE id = 1")
    streak_days, last_relapse = await get_streak(db)
    clean, elapsed, pct = await get_week_clean(db)

    events = await db.execute_fetchall(
        "SELECT date, event_type, notes FROM pmo_events WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    journal = await db.execute_fetchall(
        "SELECT date, emotions, triggers, thoughts, desired_feelings, coping_strategies "
        "FROM feniks_journal WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    pleasures = await db.execute_fetchall(
        "SELECT date, pleasure_1, pleasure_2 FROM feniks_pleasures WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    daily = await db.execute_fetchall(
        "SELECT date, used, minutes, edging, note FROM feniks_daily WHERE date >= ? ORDER BY date DESC",
        (since,),
    )
    bricks = await db.execute_fetchall(
        "SELECT id, date, hook, craving, story FROM feniks_bricks WHERE date BETWEEN ? AND ? ORDER BY date DESC, id DESC",
        (since, today.isoformat()),
    )
    bricks_total = (await db.execute_fetchall("SELECT COUNT(*) AS c FROM feniks_bricks"))[0]["c"]
    return {
        "range_days": days,
        "since": since,
        "config": dict(conf[0]) if conf else None,
        "streak_days": streak_days,
        "last_relapse": last_relapse.isoformat() if last_relapse else None,
        "week_clean": {"clean_days": clean, "days_elapsed": elapsed, "pct": pct},
        "events": [dict(r) for r in events],
        "journal": [dict(r) for r in journal],
        "pleasures": [dict(r) for r in pleasures],
        "daily": [dict(r) for r in daily],
        "bricks": [dict(r) for r in bricks],
        "bricks_total": bricks_total,
    }


# --- Exercise library: dictionary CRUD ---
# Every write below routes through app/library_validation.py's
# validate_library_write — the ONE place that decides accept/reject for this
# table (I1, 2026-07-30 review). This router's only job is to translate a
# LibraryWriteError into an HTTPException(exc.status, exc.message); settings.py
# renders the identical decision as a `?err=` redirect instead. Do not
# re-implement any of section/metric/duplicate/rename/builtin checks here —
# that duplication is exactly how the two surfaces drifted apart before.
#
# CrossFit rows (tagged 'crossfit', migration 019) are also the WOD parser's
# closed prompt vocabulary (app/services/wod_parser.py:canonical_movements) —
# editing section/metric or deleting one of those rows changes what movements
# the parser is allowed to recognise in a future WOD note. Renaming does NOT
# narrow that vocabulary (the parser reads whatever name is current) and may
# now be refused outright — see validate_library_write's I2 rename guard.
#
# Tags live in exercise_library_tags, not in exercise_library, and are
# deliberately OUTSIDE validate_library_write's scope: they're free-form
# labels, not identity/vocabulary fields, so unlike name/section/metric they
# are never THEMSELVES gated by `builtin` — a builtin row always accepts a
# tags-only change. That guard is still all-or-nothing per request, though:
# the rest of the payload (everything but `tags`) still reaches
# validate_library_write, so a PATCH that mixes `tags` with any frozen field
# (name/section/metric/reps/notes/sets) on a builtin row is refused in full
# — the tags never land either. Tag a builtin row in its own request, never
# combined with an edit to one of those fields.


# extra="forbid" does double duty: it turns an unknown key (e.g. an MCP client
# sending the retired `category` field back on a PATCH) into a loud 422
# instead of a silently-ignored no-op, and it guarantees
# `LibraryPatch.model_dump()` can only ever contain the fields declared below —
# which is what keeps the dynamic `SET {k} = ?` construction in api_library_patch
# structurally safe rather than merely safe-by-current-convention.
class LibraryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section: str
    name: str
    sets: int | None = None
    reps: str = ""
    notes: str = ""
    metric: str = "reps"
    tags: list[str] = []


class LibraryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    section: str | None = None
    sets: int | None = None
    reps: str | None = None
    notes: str | None = None
    metric: str | None = None
    archived: int | None = None
    tags: list[str] | None = None


async def _tags_by_library_id(db, ids: list[int]) -> dict[int, list[str]]:
    """Batched tag lookup for a set of library ids — one query rather than
    one-per-row, same rationale as api_training_detail's batched entries query."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = await db.execute_fetchall(
        f"SELECT library_id, tag FROM exercise_library_tags WHERE library_id IN ({placeholders})",
        ids,
    )
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["library_id"], []).append(r["tag"])
    return {k: sorted(v) for k, v in out.items()}


async def _replace_tags(db, entry_id: int, tags: list[str]) -> None:
    """Delete-then-insert the full tag set for one row, inside the caller's transaction."""
    await db.execute("DELETE FROM exercise_library_tags WHERE library_id = ?", (entry_id,))
    for tag in tags:
        await db.execute("INSERT INTO exercise_library_tags (library_id, tag) VALUES (?, ?)", (entry_id, tag))


@router.get("/library")
async def api_library_list(
    db: ApiDb,
    include_archived: bool = Query(False),
    tag: str = Query(""),
):
    """The exercise library — the dictionary the WOD parser and the picker draw from.
    ?tag= filters to entries carrying that tag (normalised the same way a write would)."""
    sql = "SELECT * FROM exercise_library WHERE 1 = 1"
    params: list = []
    if not include_archived:
        sql += " AND archived = 0"
    if tag:
        try:
            norm_tag = normalize_tag(tag)
        except LibraryWriteError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        sql += " AND id IN (SELECT library_id FROM exercise_library_tags WHERE tag = ?)"
        params.append(norm_tag)
    sql += " ORDER BY display_order, name"
    rows = await db.execute_fetchall(sql, params)
    entries = [dict(r) for r in rows]
    tags_by_id = await _tags_by_library_id(db, [e["id"] for e in entries])
    for e in entries:
        e["tags"] = tags_by_id.get(e["id"], [])
    return {"entries": entries}


@router.post("/library", status_code=201)
async def api_library_create(db: ApiDb, payload: LibraryCreate):
    try:
        row = await validate_library_write(
            db,
            op="create",
            fields={
                "name": payload.name,
                "section": payload.section,
                "sets": payload.sets,
                "reps": payload.reps,
                "notes": payload.notes,
                "metric": payload.metric,
            },
        )
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    try:
        tags = normalize_tags(payload.tags)
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) AS m FROM exercise_library")
    cursor = await db.execute(
        "INSERT INTO exercise_library (section, name, sets, reps, notes, display_order, metric, builtin) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (
            row["section"],
            row["name"],
            row["sets"],
            row["reps"],
            row["notes"],
            (order_row[0]["m"] if order_row else 0) + 1,
            row["metric"],
        ),
    )
    await _replace_tags(db, cursor.lastrowid, tags)
    await db.commit()
    return {"id": cursor.lastrowid}


@router.patch("/library/{entry_id}")
async def api_library_patch(db: ApiDb, entry_id: int, payload: LibraryPatch):
    rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"library entry {entry_id} not found")
    existing = dict(rows[0])

    fields = payload.model_dump(exclude_none=True)
    # Tags live in a separate join table and are never THEMSELVES gated by
    # `builtin` — popping `tags_raw` out here means a tags-only PATCH never
    # reaches validate_library_write's builtin guard at all. But `fields`
    # (whatever else was in the payload) still does: if it's non-empty and
    # the row is builtin, validate_library_write raises below before the
    # tags branch further down ever runs — so a PATCH combining `tags` with
    # a frozen field is rejected wholesale, tags included, not just the
    # frozen field. PATCH's semantics for tags, once they DO land, are
    # "replace the whole set", not "merge".
    tags_raw = fields.pop("tags", None)
    if not fields and tags_raw is None:
        return {"id": entry_id, "updated": []}

    try:
        result = await validate_library_write(db, op="update", entry_id=entry_id, existing=existing, fields=fields)
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    # M2 (2026-07-31 review): normalize_tags must run — and be allowed to
    # raise — BEFORE the UPDATE below, not after. This used to run after,
    # with no db.rollback() on the except branch, so a bad tag left the
    # UPDATE's effect sitting uncommitted on this request's connection. It
    # only ever looked atomic because auth.py opens a brand new connection
    # per request and closes it in a `finally` without ever committing on an
    # exception path — introduce any form of connection reuse/pooling and
    # this starts committing half of a rejected write. Validating first (same
    # order settings.py's add path already uses) makes that true regardless
    # of what closes the connection, instead of by accident.
    tags = None
    if tags_raw is not None:
        try:
            tags = normalize_tags(tags_raw)
        except LibraryWriteError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc

    if result:
        assignments = ", ".join(f"{k} = ?" for k in result)
        await db.execute(
            f"UPDATE exercise_library SET {assignments} WHERE id = ?",  # noqa: S608 — keys come from
            # validate_library_write's fixed key set (itself built from LibraryPatch's extra="forbid"
            # fields above), never attacker-controlled column names.
            [*result.values(), entry_id],
        )

    updated = set(result)
    if tags_raw is not None:
        await _replace_tags(db, entry_id, tags)
        updated.add("tags")

    await db.commit()
    return {"id": entry_id, "updated": sorted(updated)}


@router.delete("/library/{entry_id}", status_code=204)
async def api_library_delete(db: ApiDb, entry_id: int):
    rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"library entry {entry_id} not found")
    try:
        await validate_library_write(db, op="delete", entry_id=entry_id, existing=dict(rows[0]))
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    await db.execute("DELETE FROM exercise_library WHERE id = ?", (entry_id,))
    await db.commit()
