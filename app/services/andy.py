"""A.N.D.Y. daily suggestions: gather context, buy four lines, store them."""

import logging
import os
from datetime import date as date_module
from datetime import timedelta

from app.config import SECOND_BRAIN_PATH
from app.services.llm import call_llm, parse_andy_response
from app.services.training_schedule import schedule_block

logger = logging.getLogger(__name__)

DAYS_PL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ANDY_KEYS = ("andy_body_desc", "andy_spirit_desc", "andy_account_desc", "andy_relations_desc")
ANDY_VALUE_MAX = 200

_SYSTEM_PROMPT = (
    "You are a personal daily planner. Based on the user's goals, weekly plan, training schedule, "
    "and current week's data, suggest specific actions for today. "
    "Respond ONLY with valid JSON, no markdown fences. "
    'The JSON must have exactly these keys: "andy_body_desc", "andy_spirit_desc", "andy_account_desc", '
    '"andy_relations_desc". '
    "Each value should be a concise task description in English (max 60 chars)."
)


async def _context_parts(db, target_date: date_module) -> list[str]:
    context_parts: list[str] = []

    # 1. Goals context
    areas = await db.execute_fetchall("SELECT * FROM goal_areas ORDER BY display_order")
    goals = await db.execute_fetchall("SELECT * FROM goals ORDER BY area_id, horizon, display_order")
    if goals:
        goals_map: dict[tuple, list] = {}
        for g in goals:
            g = dict(g)
            goals_map.setdefault((g["area_id"], g["horizon"]), []).append(g["content"])
        goal_lines = ["--- Goals ---"]
        for a in areas:
            a = dict(a)
            for horizon in ("1yr", "3yr", "10yr"):
                items = goals_map.get((a["id"], horizon), [])
                if items:
                    goal_lines.append(f"{a['name']} ({horizon}):")
                    for item in items:
                        goal_lines.append(f"  - {item}")
        context_parts.append("\n".join(goal_lines))

    # 2. Current week daily logs
    monday = target_date - timedelta(days=target_date.weekday())
    sunday = monday + timedelta(days=6)
    week_logs = await db.execute_fetchall(
        "SELECT * FROM daily_logs WHERE date BETWEEN ? AND ? ORDER BY date",
        (monday.isoformat(), sunday.isoformat()),
    )
    if week_logs:
        week_lines = ["--- This Week ---"]
        for row in week_logs:
            r = dict(row)
            energy = r.get("energy", "?")
            week_lines.append(
                f"{r['date']}: energy={energy}, body={r.get('andy_body_desc', '')}, "
                f"spirit={r.get('andy_spirit_desc', '')}, account={r.get('andy_account_desc', '')}, "
                f"relations={r.get('andy_relations_desc', '')}"
            )
        context_parts.append("\n".join(week_lines))

    # 3. Weekly training schedule + what has actually been logged.
    # This used to list every non-archived, non-ad_hoc row of training_exercises
    # as a prescription. That block described a basement kettlebell program the
    # user no longer follows, and the rows outlive the program by design (they
    # anchor training_entries), so the staleness had no natural end. See
    # app/services/training_schedule.py.
    context_parts.append(await schedule_block(db, target_date))

    # 4. plan.md from disk (user-written, not generated)
    if SECOND_BRAIN_PATH:
        plan_path = os.path.join(SECOND_BRAIN_PATH, "plan.md")
        if os.path.isfile(plan_path):
            with open(plan_path, encoding="utf-8") as f:
                context_parts.append(f"--- plan.md ---\n{f.read()[:3000]}")

    return context_parts


async def generate_andy_suggestions(db, day_iso: str) -> dict[str, str]:
    """Buy four suggestions for one day. Reads only, so nothing to roll back."""
    target_date = date_module.fromisoformat(day_iso)
    user_parts = [f"Date: {day_iso} ({DAYS_PL[target_date.weekday()]})\n"]
    for part in await _context_parts(db, target_date):
        user_parts.append(part + "\n")

    # Generous max_tokens: when litellm cannot map reasoning_effort for a model
    # (e.g. newer Gemini flashes), drop_params discards the flag and the model
    # thinks unbounded — a 2048 budget then truncates mid-JSON.
    raw = await call_llm(db, _SYSTEM_PROMPT, "\n".join(user_parts), json_mode=True, max_tokens=8192)
    data = parse_andy_response(raw)
    suggestions = {key: str(data.get(key) or "").strip()[:ANDY_VALUE_MAX] for key in ANDY_KEYS}
    if not any(suggestions.values()):
        raise ValueError("AI returned no suggestions (empty response)")
    return suggestions


async def save_andy_suggestions(db, day_iso: str, suggestions: dict[str, str]) -> int:
    """Store one day's suggestions. The caller owns the transaction, because the
    suggestions and their publication marker have to commit together."""
    await db.execute(
        """
        INSERT INTO daily_logs (date, andy_body_desc, andy_spirit_desc, andy_account_desc, andy_relations_desc)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            andy_body_desc=excluded.andy_body_desc, andy_spirit_desc=excluded.andy_spirit_desc,
            andy_account_desc=excluded.andy_account_desc, andy_relations_desc=excluded.andy_relations_desc,
            updated_at=datetime('now')
        """,
        (day_iso, *(suggestions.get(key, "") for key in ANDY_KEYS)),
    )
    return sum(1 for key in ANDY_KEYS if suggestions.get(key))
