"""REST API for machine-to-machine access (OpenClaw, AI agents, scripts).

Auth: a secret in `X-API-Key` or `Authorization: Bearer`. It is either the
server's VIRGIL_API_KEY, which serves exactly one account named by
VIRGIL_API_USER_EMAIL (an empty email, or a missing or deactivated account,
returns 503 instead of serving someone else), or an account's personal token,
which serves that account with the scopes it was created with. The API is
disabled while neither exists. `/mcp` uses this same resolution. Writes cover canonical goals/reps, experiment logging,
exercise-library CRUD,
the daily log (habits and A.N.D.Y.), Freedom (/api/freedom: habits, their days, bricks and
settings; the /api/feniks paths are deprecated aliases), training
sessions, body measurements and the optional modules' flags (/api/features).
"""

import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.central_db import get_central_db
from app.config import API_KEY, API_USER_EMAIL
from app.db import FEATURE_FLAGS, get_feature_flags, set_setting
from app.library_validation import LibraryWriteError, normalize_tag
from app.services.daily_log import DailyLogError, delete_daily_log, get_daily_log, upsert_daily_log
from app.services.experiment_weeks import experiment_calendar, experiment_week_number
from app.services.freedom_log import (
    FreedomWriteError,
    add_brick,
    delete_brick,
    habit_overview,
    save_day,
    update_brick,
)
from app.services.freedom_log import delete_day as delete_freedom_day
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
from app.services.habits import HabitError, list_habits, resolve_habit, update_habit
from app.services.library import write_library
from app.services.measurements import MeasurementError, delete_measurements, list_measurements, upsert_measurements
from app.services.movement_vocab import EQUIPMENT, aliases_by_library_id
from app.services.streak import get_streak, get_week_clean
from app.services.training_log import (
    TrainingWriteError,
    create_session,
    delete_session,
    session_summary,
    update_session,
)
from app.user_db import close_user_db, open_user_db
from app.validation import clamp_metric_value, truncate, valid_date

logger = logging.getLogger(__name__)
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


@dataclass(frozen=True)
class ApiCredential:
    """Who a request acts for, and what it may do."""

    user_id: str
    db_filename: str
    scopes: frozenset[str]
    # The server's VIRGIL_API_KEY rather than a personal token: its scopes come
    # from VIRGIL_API_SENSITIVE and VIRGIL_API_FINANCE, so refusals name those.
    server_key: bool


def presented_secret(headers) -> str:
    """The secret in X-API-Key, else in Authorization: Bearer. Two different ones are refused."""
    key = headers.get("x-api-key", "")
    authorization = headers.get("authorization", "")
    bearer = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
    if key and bearer and not hmac.compare_digest(key.encode(), bearer.encode()):
        raise HTTPException(status_code=401, detail="X-API-Key and Authorization carry different credentials")
    return key or bearer


async def authenticate(secret: str) -> ApiCredential:
    """Resolve a secret to the one account it serves; shared by the REST routes and /mcp.

    The server key and the token store are both consulted every time, so the
    response time does not say which one a secret belongs to."""
    from app.central_db import any_live_api_token, find_api_token, touch_api_token

    server_key = bool(API_KEY) and hmac.compare_digest(secret.encode(), API_KEY.encode())
    token = await find_api_token(secret) if secret else None
    if server_key:
        if not API_USER_EMAIL:
            raise HTTPException(
                status_code=503,
                detail="API user not configured (set VIRGIL_API_USER_EMAIL to the account this key should serve)",
            )
        from app.server_settings import current

        central = await get_central_db()
        rows = await central.execute_fetchall(
            "SELECT id, db_filename FROM users WHERE email = ? AND is_active = 1",
            (API_USER_EMAIL.lower(),),
        )
        if not rows:
            raise HTTPException(status_code=503, detail="API user not found")
        scopes = {"read", "write"}
        scopes |= {"sensitive"} if current.API_SENSITIVE else set()
        scopes |= {"finance"} if current.API_FINANCE else set()
        return ApiCredential(rows[0]["id"], rows[0]["db_filename"], frozenset(scopes), server_key=True)
    if token:
        await touch_api_token(token["id"], datetime.now(UTC))
        return ApiCredential(
            token["user_id"], token["db_filename"], frozenset(token["scopes"].split()), server_key=False
        )
    if not API_KEY and not await any_live_api_token():
        raise HTTPException(status_code=403, detail="API disabled (no VIRGIL_API_KEY and no personal tokens)")
    raise HTTPException(status_code=401, detail="Invalid API key")


# What the server key needs for each scope it may lack.
_SERVER_KEY_SCOPE = {
    "sensitive": "Sensitive scope disabled (set VIRGIL_API_SENSITIVE=true to use Freedom data)",
    "finance": "Finance scope disabled (set VIRGIL_API_FINANCE=true to use finance data)",
}


def require_scope(request: Request, scope: str) -> None:
    credential: ApiCredential = request.state.api_credential
    if scope in credential.scopes:
        return
    if credential.server_key and scope in _SERVER_KEY_SCOPE:
        raise HTTPException(status_code=403, detail=_SERVER_KEY_SCOPE[scope])
    raise HTTPException(status_code=403, detail=f"This token lacks the {scope} scope")


def warn_if_api_user_unconfigured() -> None:
    """Log at startup when a key is set without the account it must serve.

    The key used to follow the earliest active admin, so disabling that account
    or adding another admin moved it onto someone else's data. Empty email now
    fails closed on each request; this line is how an existing deploy notices.
    """
    if API_KEY and not API_USER_EMAIL:
        logger.error(
            "VIRGIL_API_KEY is set but VIRGIL_API_USER_EMAIL is empty. "
            "API and /mcp requests will return 503 until it names the account the key should serve. "
            "The key no longer falls back to the first active admin."
        )


async def api_db(request: Request):
    """Authenticate, check read or write by method, and yield that account's DB connection."""
    credential = await authenticate(presented_secret(request.headers))
    request.state.api_credential = credential
    require_scope(request, "read" if request.method in ("GET", "HEAD") else "write")

    db = await open_user_db(credential.db_filename)
    try:
        yield db
    finally:
        await close_user_db(db)


ApiDb = Annotated[aiosqlite.Connection, Depends(api_db)]


async def sensitive_db(request: Request, db: ApiDb):
    """Freedom data: the sensitive scope, checked before the body is validated."""
    require_scope(request, "sensitive")
    return db


SensitiveDb = Annotated[aiosqlite.Connection, Depends(sensitive_db)]


@router.get("/summary")
async def api_summary(request: Request, db: ApiDb):
    """Today's snapshot: daily habits, the first Freedom habit's streak (key "feniks", kept for
    older readers; null unless the sensitive scope is on, since the habit can be any
    addiction), latest Oura, training this week, latest measurements."""

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

    freedom = None
    if "sensitive" in request.state.api_credential.scopes:
        try:
            first_habit = await resolve_habit(db)
            streak_days, last_relapse = await get_streak(db, first_habit["id"])
        except LookupError:
            streak_days, last_relapse = 0, None
        freedom = {"streak_days": streak_days, "last_relapse": last_relapse.isoformat() if last_relapse else None}

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
        "feniks": freedom,
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
    entry_date = valid_date(entry_date)
    if not entry_date:
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
            f"e.set_number, e.reps, e.weight, e.duration, e.notes, "
            f"COALESCE(e.equipment, lib.default_equipment) AS equipment, fam.name AS family "
            f"FROM training_entries e JOIN training_exercises ex ON e.exercise_id = ex.id "
            f"LEFT JOIN exercise_library lib ON lower(lib.name) = lower(ex.name) "
            f"LEFT JOIN exercise_families fam ON fam.id = lib.family_id "
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
                    "family": r["family"],
                    "sets": [],
                }
                order.append(ex_id)
            exercises[ex_id]["sets"].append(
                {
                    "set": r["set_number"],
                    "reps": r["reps"],
                    "weight": r["weight"],
                    "duration": r["duration"],
                    "equipment": r["equipment"],
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
    db: SensitiveDb,
    days: int = Query(30, ge=1, le=365, alias="range"),
):
    """Freedom's Porn habit in detail: config, streak, current-week clean rate (Gola), plus the
    relapse/reset events, journal entries (emotions/triggers/thoughts/coping) and logged
    pleasures from the last N days (?range=30). This is the WHY behind the streak.

    Needs the sensitive scope: this is intimate journal content, and a leaked
    API key must not expose it by default."""
    today = date.today()
    since = (today - timedelta(days=days - 1)).isoformat()

    porn = await db.execute_fetchall("SELECT id, start_date, target_days, big_why FROM habits WHERE slug = 'porn'")
    habit_id = porn[0]["id"] if porn else -1
    conf = [{key: porn[0][key] for key in ("start_date", "target_days", "big_why")}] if porn else []
    streak_days, last_relapse = await get_streak(db, habit_id)
    clean, elapsed, pct = await get_week_clean(db, habit_id)

    events = await db.execute_fetchall(
        "SELECT date, event_type, notes FROM pmo_events WHERE habit_id = ? AND date >= ? ORDER BY date DESC",
        (habit_id, since),
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
    # The legacy field names: this endpoint's readers predate Freedom.
    daily = await db.execute_fetchall(
        "SELECT date, slipped AS used, amount AS minutes, near_miss AS edging, note FROM feniks_daily "
        "WHERE habit_id = ? AND date >= ? ORDER BY date DESC",
        (habit_id, since),
    )
    bricks = await db.execute_fetchall(
        "SELECT id, date, hook, craving, story FROM feniks_bricks "
        "WHERE habit_id = ? AND date BETWEEN ? AND ? ORDER BY date DESC, id DESC",
        (habit_id, since, today.isoformat()),
    )
    bricks_total = (
        await db.execute_fetchall("SELECT COUNT(*) AS c FROM feniks_bricks WHERE habit_id = ?", (habit_id,))
    )[0]["c"]
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
    family: str | None = None
    default_equipment: str | None = None
    aliases: list[str] = []


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
    family: str | None = None
    default_equipment: str | None = None
    aliases: list[str] | None = None


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


@router.get("/library")
async def api_library_list(
    db: ApiDb,
    include_archived: bool = Query(False),
    tag: str = Query(""),
):
    """The exercise library — the dictionary the WOD parser and the picker draw from.
    ?tag= filters to entries carrying that tag (normalised the same way a write would)."""
    sql = (
        "SELECT l.*, f.name AS family FROM exercise_library l "
        "LEFT JOIN exercise_families f ON f.id = l.family_id WHERE 1 = 1"
    )
    params: list = []
    if not include_archived:
        sql += " AND l.archived = 0"
    if tag:
        try:
            norm_tag = normalize_tag(tag)
        except LibraryWriteError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        sql += " AND l.id IN (SELECT library_id FROM exercise_library_tags WHERE tag = ?)"
        params.append(norm_tag)
    sql += " ORDER BY l.display_order, l.name"
    rows = await db.execute_fetchall(sql, params)
    entries = [dict(r) for r in rows]
    tags_by_id = await _tags_by_library_id(db, [e["id"] for e in entries])
    aliases_by_id = await aliases_by_library_id(db, [e["id"] for e in entries])
    for e in entries:
        e["tags"] = tags_by_id.get(e["id"], [])
        e["aliases"] = aliases_by_id.get(e["id"], [])
        e.pop("family_id", None)
    families = await db.execute_fetchall("SELECT name FROM exercise_families ORDER BY display_order")
    return {"entries": entries, "families": [row["name"] for row in families], "equipment": list(EQUIPMENT)}


@router.post("/library", status_code=201)
async def api_library_create(db: ApiDb, payload: LibraryCreate):
    fields = payload.model_dump(exclude={"tags", "family", "default_equipment", "aliases"})
    classification = payload.model_dump(include={"family", "default_equipment", "aliases"})
    try:
        result = await write_library(db, op="create", fields=fields, tags=payload.tags, classification=classification)
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc
    return {"id": result["id"]}


@router.patch("/library/{entry_id}")
async def api_library_patch(db: ApiDb, entry_id: int, payload: LibraryPatch):
    sent = payload.model_dump(exclude_unset=True)
    # An explicit null clears a classification; the other fields ignore null as before.
    classification = {key: sent.pop(key) for key in ("family", "default_equipment", "aliases") if key in sent}
    fields = {key: value for key, value in sent.items() if value is not None}
    tags = fields.pop("tags", None)
    try:
        return await write_library(
            db, op="update", entry_id=entry_id, fields=fields, tags=tags, classification=classification
        )
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.delete("/library/{entry_id}", status_code=204)
async def api_library_delete(db: ApiDb, entry_id: int):
    try:
        await write_library(db, op="delete", entry_id=entry_id)
    except LibraryWriteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


# --- Daily log, Feniks and training writes -------------------------------------
# A morning check-in agent records yesterday's day without opening the forms.


def _iso_day(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="date must be an ISO date (YYYY-MM-DD)") from exc


async def _habit(db, slug: str | None) -> dict:
    """The habit a Freedom call is about: ?habit=<slug>, else the first one tracked."""
    try:
        return await resolve_habit(db, slug)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


HabitParam = Annotated[str | None, Query(description="Habit slug; defaults to the first tracked habit")]


@router.get("/freedom")
async def api_freedom(db: SensitiveDb):
    """Freedom: every tracked habit with where it stands today (streak, this week,
    today's entry, urges survived) and its own definition of a slip."""
    habits = []
    for habit in await list_habits(db):
        definition = {
            key: habit[key]
            for key in ("slip_label", "slip_definition", "amount_unit", "slip_threshold", "near_miss_label")
        }
        settings = {key: habit[key] for key in ("start_date", "target_days", "big_why")}
        habits.append({**await habit_overview(db, habit), "definition": definition, "settings": settings})
    return {"habits": habits}


class ApiDailyPatch(BaseModel):
    """Only the fields sent are changed; the rest of the day keeps its value."""

    model_config = ConfigDict(extra="forbid")

    energy: int | None = None
    morning_routine: str | None = None
    evening_routine: str | None = None
    water: str | None = None
    andy_body_status: str | None = None
    andy_body_desc: str | None = None
    andy_spirit_status: str | None = None
    andy_spirit_desc: str | None = None
    andy_account_status: str | None = None
    andy_account_desc: str | None = None
    andy_relations_status: str | None = None
    andy_relations_desc: str | None = None
    notes: str | None = None


@router.put("/daily/{day}")
async def api_put_daily(day: str, payload: ApiDailyPatch, db: ApiDb):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one daily-log field")
    try:
        return await upsert_daily_log(db, _iso_day(day), changes)
    except DailyLogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ApiFreedomDay(BaseModel):
    """One habit's day. slipped and near_miss are separate; amount is kept only for a slip.

    A near miss with slipped false is stored and is not a clean day. It does not
    write a relapse marker.
    """

    model_config = ConfigDict(extra="forbid")

    slipped: bool
    amount: int | None = None
    near_miss: bool = False
    note: str = ""


class ApiFeniksDay(ApiFreedomDay):
    """The deprecated /api/feniks/daily body: the No Porn names (used, minutes, edging) still work."""

    slipped: bool = Field(validation_alias=AliasChoices("slipped", "used"))
    amount: int | None = Field(None, validation_alias=AliasChoices("amount", "minutes"))
    near_miss: bool = Field(False, validation_alias=AliasChoices("near_miss", "edging"))


class ApiBrick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    hook: str
    craving: int | None = None
    story: str = ""


async def _put_day(db, target: dict, day: str, payload: ApiFreedomDay) -> dict:
    try:
        return await save_day(
            db,
            target,
            day,
            slipped=payload.slipped,
            amount=payload.amount,
            near_miss=payload.near_miss,
            note=payload.note,
        )
    except FreedomWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _add_brick(db, target: dict, payload: ApiBrick) -> dict:
    try:
        return await add_brick(
            db, target, payload.date, hook=payload.hook, craving=payload.craving, story=payload.story
        )
    except FreedomWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/freedom/{habit}/days/{day}")
async def api_put_freedom_day(habit: str, day: str, payload: ApiFreedomDay, db: SensitiveDb):
    """Record one habit's day, replacing that day's entry. A slip marks a relapse for the
    habit's streak; correcting it to clean removes only that marker."""
    return await _put_day(db, await _habit(db, habit), day, payload)


@router.post("/freedom/{habit}/bricks", status_code=201)
async def api_add_freedom_brick(habit: str, payload: ApiBrick, db: SensitiveDb):
    """One urge survived for one habit."""
    return await _add_brick(db, await _habit(db, habit), payload)


@router.put("/feniks/daily/{day}", deprecated=True)
async def api_put_feniks_day(day: str, payload: ApiFeniksDay, db: SensitiveDb, habit: HabitParam = None):
    """Deprecated: PUT /api/freedom/{habit}/days/{day}."""
    return await _put_day(db, await _habit(db, habit), day, payload)


@router.post("/feniks/bricks", status_code=201, deprecated=True)
async def api_add_brick(payload: ApiBrick, db: SensitiveDb, habit: HabitParam = None):
    """Deprecated: POST /api/freedom/{habit}/bricks."""
    return await _add_brick(db, await _habit(db, habit), payload)


class ApiTrainingEntry(BaseModel):
    """One set. `movement` is a library name or alias; `duration` is seconds.
    `equipment` is set only when it differs from the movement's usual one."""

    model_config = ConfigDict(extra="forbid")

    movement: str
    set_number: int = 1
    reps: int | None = None
    weight: float | None = None
    duration: float | None = None
    equipment: str | None = None
    note: str = ""


class ApiTrainingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str
    duration_minutes: int | None = None
    notes: str = ""
    entries: list[ApiTrainingEntry] = []
    idempotency_key: str | None = None


class ApiTrainingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    duration_minutes: int | None = None
    notes: str | None = None
    entries: list[ApiTrainingEntry] | None = None


def _training_error(exc: TrainingWriteError) -> HTTPException:
    if exc.unknown_movements:
        return HTTPException(
            status_code=exc.status, detail={"message": str(exc), "unknown_movements": exc.unknown_movements}
        )
    return HTTPException(status_code=exc.status, detail=str(exc))


@router.post("/training/sessions")
async def api_create_training(payload: ApiTrainingCreate, db: ApiDb):
    try:
        session, created = await create_session(
            db,
            day=_iso_day(payload.date),
            duration_minutes=payload.duration_minutes,
            notes=payload.notes,
            entries=[entry.model_dump() for entry in payload.entries],
            idempotency_key=truncate(payload.idempotency_key or "", 64) or None,
        )
    except TrainingWriteError as exc:
        raise _training_error(exc) from exc
    session["created"] = created
    return session


@router.patch("/training/sessions/{session_id}")
async def api_update_training(session_id: int, payload: ApiTrainingPatch, db: ApiDb):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one session field")
    if "date" in changes:
        if changes["date"] is None:
            raise HTTPException(status_code=422, detail="date cannot be null")
        changes["date"] = _iso_day(changes["date"])
    try:
        return await update_session(db, session_id, changes)
    except TrainingWriteError as exc:
        raise _training_error(exc) from exc


@router.delete("/training/sessions/{session_id}")
async def api_delete_training(session_id: int, db: ApiDb):
    try:
        await delete_session(db, session_id)
    except TrainingWriteError as exc:
        raise _training_error(exc) from exc
    return {"deleted": True, "session_id": session_id}


# --- Remaining reads and deletes for the same records -------------------------


@router.get("/daily/{day}")
async def api_get_daily(day: str, db: ApiDb):
    log = await get_daily_log(db, _iso_day(day))
    if log is None:
        raise HTTPException(status_code=404, detail="No daily log for that date")
    return log


@router.delete("/daily/{day}")
async def api_delete_daily(day: str, db: ApiDb):
    if not await delete_daily_log(db, _iso_day(day)):
        raise HTTPException(status_code=404, detail="No daily log for that date")
    return {"deleted": True, "date": _iso_day(day)}


async def _delete_day(db, target: dict, day: str) -> dict:
    try:
        await delete_freedom_day(db, target, _iso_day(day))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True, "habit": target["slug"], "date": _iso_day(day)}


@router.delete("/freedom/{habit}/days/{day}")
async def api_delete_freedom_day(habit: str, day: str, db: SensitiveDb):
    """Delete one habit's day entry and the relapse marker it created."""
    return await _delete_day(db, await _habit(db, habit), day)


@router.delete("/feniks/daily/{day}", deprecated=True)
async def api_delete_feniks_day(day: str, db: SensitiveDb, habit: HabitParam = None):
    """Deprecated: DELETE /api/freedom/{habit}/days/{day}."""
    return await _delete_day(db, await _habit(db, habit), day)


class ApiBrickPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str | None = None
    hook: str | None = None
    craving: int | None = None
    story: str | None = None


@router.patch("/freedom/bricks/{brick_id}")
@router.patch("/feniks/bricks/{brick_id}", deprecated=True)
async def api_update_brick(brick_id: int, payload: ApiBrickPatch, db: SensitiveDb):
    """Edit a brick; /api/feniks/bricks/{brick_id} is the deprecated path."""
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one brick field")
    try:
        return await update_brick(db, brick_id, changes)
    except FreedomWriteError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/freedom/bricks/{brick_id}")
@router.delete("/feniks/bricks/{brick_id}", deprecated=True)
async def api_delete_brick(brick_id: int, db: SensitiveDb):
    """Delete a brick; /api/feniks/bricks/{brick_id} is the deprecated path."""
    try:
        await delete_brick(db, brick_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": True, "brick_id": brick_id}


class ApiFeniksConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: str | None = None
    target_days: int | None = None
    big_why: str | None = None


class ApiHabitPatch(BaseModel):
    """Only the fields sent change. A habit's slug stays the same when it is renamed."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    slip_label: str | None = None
    slip_definition: str | None = None
    amount_unit: str | None = None
    slip_threshold: int | None = None
    near_miss_label: str | None = None
    start_date: str | None = None
    target_days: int | None = None
    big_why: str | None = None


@router.patch("/freedom/{habit}")
async def api_update_habit(habit: str, payload: ApiHabitPatch, db: SensitiveDb):
    """One habit's settings: its name, its definition of a slip, start date, target and why."""
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one habit field")
    target = await _habit(db, habit)
    try:
        return await update_habit(db, target["slug"], changes)
    except HabitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/feniks/config", deprecated=True)
async def api_update_feniks_config(payload: ApiFeniksConfig, db: SensitiveDb, habit: HabitParam = None):
    """Deprecated: PATCH /api/freedom/{habit}. One habit's start date, target and "why"."""
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one config field")
    target = await _habit(db, habit)
    try:
        updated = await update_habit(db, target["slug"], changes)
    except HabitError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {key: updated[key] for key in ("slug", "start_date", "target_days", "big_why")}


@router.get("/training/sessions/{session_id}")
async def api_get_training(session_id: int, db: ApiDb):
    rows = await db.execute_fetchall("SELECT 1 FROM training_sessions WHERE id = ?", (session_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Training session not found")
    return await session_summary(db, session_id)


class ApiMeasurements(BaseModel):
    """Weight in kg, circumferences in cm. Only the fields sent change; null clears one."""

    model_config = ConfigDict(extra="forbid")

    weight: float | None = None
    arm: float | None = None
    waist: float | None = None
    hips: float | None = None
    thighs: float | None = None


@router.get("/measurements")
async def api_measurements(db: ApiDb, days: int = Query(90, ge=1, le=3650, alias="range")):
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    return {"range_days": days, "since": since, "measurements": await list_measurements(db, since)}


@router.put("/measurements/{day}")
async def api_put_measurements(day: str, payload: ApiMeasurements, db: ApiDb):
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="Provide at least one measurement")
    try:
        return await upsert_measurements(db, _iso_day(day), changes)
    except MeasurementError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/measurements/{day}")
async def api_delete_measurements(day: str, db: ApiDb):
    if not await delete_measurements(db, _iso_day(day)):
        raise HTTPException(status_code=404, detail="No measurements for that date")
    return {"deleted": True, "date": _iso_day(day)}


# --- Feature flags: only the modules Settings offers, each behind its data's scope ---


class ApiFeature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get("/features")
async def api_features(request: Request, db: ApiDb):
    """Each module's flag; null where the token lacks the scope that guards its data,
    since even knowing that Freedom is on says something intimate."""
    scopes = request.state.api_credential.scopes
    stored = await get_feature_flags(db)
    return {
        "features": {
            name: stored.get(name, False) if scope in scopes else None for name, scope in FEATURE_FLAGS.items()
        }
    }


async def feature_db(feature: str, request: Request, db: ApiDb):
    """A known flag, then the scope of the data it reveals, both before the body is validated."""
    if feature not in FEATURE_FLAGS:
        raise HTTPException(status_code=404, detail=f"Unknown feature; known: {', '.join(sorted(FEATURE_FLAGS))}")
    require_scope(request, FEATURE_FLAGS[feature])
    return db


@router.put("/features/{feature}")
async def api_set_feature(feature: str, payload: ApiFeature, db: Annotated[aiosqlite.Connection, Depends(feature_db)]):
    await set_setting(db, f"feature_{feature}", "1" if payload.enabled else "0")
    return {"name": feature, "enabled": payload.enabled}


# --- Finance ---


async def finance_db(request: Request, db: ApiDb):
    """The key first, then the finance scope, both before the body is validated."""
    require_scope(request, "finance")
    return db


FinanceDb = Annotated[aiosqlite.Connection, Depends(finance_db)]


async def _finance_call(call):
    from app.services.finance import FinanceError, FinanceNotFound

    try:
        return await call
    except FinanceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FinanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class ApiFinanceBalances(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[dict]


class ApiFinanceAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    name: str | None = None
    layer: int | str | None = None
    institution: str | None = None
    kind: str | None = None
    currency: str | None = None
    ownership: str | None = None
    archived: bool | None = None


class ApiFinanceAssumptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    essential_monthly_cost: float | str | None = None
    effective_from: str | None = None
    income_sources: list[dict] | None = None
    policies: list[dict] | None = None
    base_currency: str | None = None


class ApiFinanceScoring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: list[dict] | None = None
    remove: list[int | str] | None = None


@router.get("/finance")
async def api_finance(db: FinanceDb, day: str | None = Query(None, alias="date")):
    """The fortress on a date (default today): layers with their accounts and
    latest balances, the cushion and how many months of essential cost it covers,
    income shares, policies and the score."""
    from app.services.finance import overview

    return await _finance_call(overview(db, day))


@router.get("/finance/history")
async def api_finance_history(db: FinanceDb, months: int = Query(12, ge=1, le=240)):
    from app.services.finance import history

    return await _finance_call(history(db, months))


@router.put("/finance/balances/{day}")
async def api_finance_balances(day: str, payload: ApiFinanceBalances, db: FinanceDb):
    """Balances of several accounts on one date, all or none. A null amount removes one."""
    from app.services.finance import record_balances

    return await _finance_call(record_balances(db, day, payload.entries, source="api"))


@router.get("/finance/accounts")
async def api_finance_accounts(db: FinanceDb):
    from app.services.finance import list_accounts

    return {"accounts": await _finance_call(list_accounts(db))}


@router.put("/finance/accounts")
async def api_finance_account(payload: ApiFinanceAccount, db: FinanceDb):
    """Create an account, or change the one named by id (or by name)."""
    from app.services.finance import upsert_account

    return await _finance_call(upsert_account(db, payload.model_dump(exclude_unset=True)))


@router.get("/finance/assumptions")
async def api_finance_assumptions(db: FinanceDb):
    from app.services.finance import assumptions

    return await _finance_call(assumptions(db))


@router.put("/finance/assumptions")
async def api_finance_set_assumptions(payload: ApiFinanceAssumptions, db: FinanceDb):
    from app.services.finance import set_assumptions

    return await _finance_call(set_assumptions(db, payload.model_dump(exclude_unset=True)))


@router.get("/finance/scoring")
async def api_finance_scoring(db: FinanceDb):
    from app.services.finance import scoring

    return await _finance_call(scoring(db))


@router.put("/finance/scoring")
async def api_finance_set_scoring(payload: ApiFinanceScoring, db: FinanceDb):
    from app.services.finance import set_scoring

    return await _finance_call(set_scoring(db, payload.layers, payload.remove))
