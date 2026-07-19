"""REST API for machine-to-machine access (OpenClaw, AI agents, scripts).

Auth: `X-API-Key` header, compared in constant time against VIRGIL_API_KEY.
The key maps to a single user's database: VIRGIL_API_USER_EMAIL if set,
otherwise the first active admin account. API is disabled when VIRGIL_API_KEY is empty.
GET endpoints are read-only. The single write is
POST /api/experiments/{id}/entries — experiment logging for MCP clients.
"""

import hmac
from datetime import date, timedelta
from typing import Annotated
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.central_db import get_central_db
from app.config import API_KEY, API_USER_EMAIL
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
        end = start + timedelta(weeks=exp["num_weeks"]) - timedelta(days=1)
        week_no = max(1, min(((today - start).days // 7) + 1, exp["num_weeks"]))
        week_start = start + timedelta(days=(week_no - 1) * 7)
        week_end = week_start + timedelta(days=6)

        target_rows = await db.execute_fetchall(
            "SELECT label, target_min, target_max FROM experiment_weeks WHERE experiment_id = ? AND week_number = ?",
            (exp["id"], week_no),
        )
        logged = await db.execute_fetchall(
            "SELECT COALESCE(SUM(CASE WHEN eat.kind = 'duration' THEN ee.value ELSE 0 END), 0) AS total, "
            "COUNT(*) AS entries "
            "FROM experiment_entries ee JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id "
            "WHERE ee.experiment_id = ? AND ee.date BETWEEN ? AND ?",
            (exp["id"], week_start.isoformat(), week_end.isoformat()),
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
                    "logged_week": _metric_logged(m["kind"], mine, week_start.isoformat(), week_end.isoformat()),
                    "logged_total": _metric_logged(m["kind"], mine, start.isoformat(), end.isoformat()),
                }
            )

        result.append(
            {
                "id": exp["id"],
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

    if isinstance(payload.metric, int) or (isinstance(payload.metric, str) and payload.metric.isdigit()):
        metric_rows = await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types WHERE id = ? AND experiment_id = ?",
            (int(payload.metric), experiment_id),
        )
    else:
        metric_rows = await db.execute_fetchall(
            "SELECT * FROM experiment_activity_types WHERE experiment_id = ? AND LOWER(name) = LOWER(?)",
            (experiment_id, payload.metric.strip()),
        )
    if not metric_rows:
        raise HTTPException(status_code=404, detail="Metric not found in this experiment")
    metric = dict(metric_rows[0])

    entry_date = payload.date or date.today().isoformat()
    if not valid_date(entry_date):
        raise HTTPException(status_code=422, detail="Invalid date (expected YYYY-MM-DD)")
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
            f"e.set_number, e.reps, e.weight, e.duration "
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
                {"set": r["set_number"], "reps": r["reps"], "weight": r["weight"], "duration": r["duration"]}
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
    }
