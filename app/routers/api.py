"""Read-only REST API for machine-to-machine access (OpenClaw, AI agents, scripts).

Auth: `X-API-Key` header, compared in constant time against VIRGIL_API_KEY.
The key maps to a single user's database: VIRGIL_API_USER_EMAIL if set,
otherwise the first active admin account. API is disabled when VIRGIL_API_KEY is empty.
All endpoints are GET — this API never mutates data.
"""

import hmac
from datetime import date, timedelta
from typing import Annotated

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.central_db import get_central_db
from app.config import API_KEY, API_USER_EMAIL
from app.services.streak import get_streak
from app.user_db import close_user_db, open_user_db

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


@router.get("/experiments/active")
async def api_experiments_active(db: ApiDb):
    """Active experiments with current-week target vs logged minutes."""
    today = date.today()
    exps = await db.execute_fetchall("SELECT * FROM experiments WHERE status = 'active' ORDER BY start_date")
    result = []
    for row in exps:
        exp = dict(row)
        start = date.fromisoformat(exp["start_date"])
        week_no = max(1, min(((today - start).days // 7) + 1, exp["num_weeks"]))
        week_start = start + timedelta(days=(week_no - 1) * 7)
        week_end = week_start + timedelta(days=6)

        target_rows = await db.execute_fetchall(
            "SELECT label, target_min, target_max FROM experiment_weeks WHERE experiment_id = ? AND week_number = ?",
            (exp["id"], week_no),
        )
        logged = await db.execute_fetchall(
            "SELECT COALESCE(SUM(duration_minutes), 0) AS total, COUNT(*) AS entries "
            "FROM experiment_entries WHERE experiment_id = ? AND date BETWEEN ? AND ?",
            (exp["id"], week_start.isoformat(), week_end.isoformat()),
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
            }
        )
    return {"experiments": result}


@router.get("/training")
async def api_training(
    db: ApiDb,
    days: int = Query(7, ge=1, le=90, alias="range"),
):
    """Training sessions in the last N days with entry counts and core volume."""
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = await db.execute_fetchall(
        "SELECT s.id, s.date, s.duration_minutes, s.notes, "
        "COUNT(e.id) AS entries, COALESCE(SUM(e.reps * COALESCE(e.weight, 0)), 0) AS volume_kg "
        "FROM training_sessions s LEFT JOIN training_entries e ON e.session_id = s.id "
        "WHERE s.date >= ? GROUP BY s.id ORDER BY s.date DESC",
        (since,),
    )
    return {"range_days": days, "since": since, "sessions": [dict(r) for r in rows]}
