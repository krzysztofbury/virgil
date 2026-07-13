import logging
import time
from datetime import date, timedelta

from app.services.llm import call_llm, llm_available

# Per-experiment cooldown to avoid hammering LLM on repeated page loads
_last_attempt: dict[int, float] = {}
_COOLDOWN_SECONDS = 300  # 5 minutes

logger = logging.getLogger(__name__)


async def has_llm(db) -> bool:
    # Includes the internal env-var fallback, not just DB-configured providers.
    return await llm_available(db)


async def get_existing_summaries(db, experiment_id: int) -> dict[int, str]:
    rows = await db.execute_fetchall(
        "SELECT week_number, summary FROM experiment_summaries WHERE experiment_id = ?",
        (experiment_id,),
    )
    return {r["week_number"]: r["summary"] for r in rows}


async def generate_week_summary(db, experiment_id: int, week_number: int) -> str:
    """Collect cross-board metrics for a given experiment week and call LLM."""
    exp_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not exp_rows:
        raise ValueError("Experiment not found")
    exp = dict(exp_rows[0])

    start = date.fromisoformat(exp["start_date"])
    start_monday = start - timedelta(days=start.weekday())
    week_start = start_monday + timedelta(weeks=week_number - 1)
    week_end = week_start + timedelta(days=6)

    # Collect experiment entries for this week
    entries = await db.execute_fetchall(
        """SELECT ee.date, ee.duration_minutes, ee.notes, ee.source,
                  eat.name as activity_name
           FROM experiment_entries ee
           JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id
           WHERE ee.experiment_id = ? AND ee.date >= ? AND ee.date <= ?
           ORDER BY ee.date""",
        (experiment_id, week_start.isoformat(), week_end.isoformat()),
    )
    entries = [dict(e) for e in entries]

    # Week targets
    week_cfg = await db.execute_fetchall(
        "SELECT * FROM experiment_weeks WHERE experiment_id = ? AND week_number = ?",
        (experiment_id, week_number),
    )
    week_cfg = dict(week_cfg[0]) if week_cfg else {"target_min": 0, "target_max": 0, "label": ""}

    # Oura daily data for the week
    oura = await db.execute_fetchall(
        "SELECT * FROM oura_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (week_start.isoformat(), week_end.isoformat()),
    )
    oura = [dict(r) for r in oura]

    # Daily logs (energy, routines)
    daily_logs = await db.execute_fetchall(
        "SELECT * FROM daily_logs WHERE date >= ? AND date <= ? ORDER BY date",
        (week_start.isoformat(), week_end.isoformat()),
    )
    daily_logs = [dict(r) for r in daily_logs]

    # Body measurements
    measurements = await db.execute_fetchall(
        "SELECT * FROM body_measurements WHERE date >= ? AND date <= ? ORDER BY date",
        (week_start.isoformat(), week_end.isoformat()),
    )
    measurements = [dict(r) for r in measurements]

    # Is this the final week?
    is_final = week_number == exp["num_weeks"]

    # Build the prompt
    total_mins = sum(e["duration_minutes"] for e in entries)
    entries_text = (
        "\n".join(
            f"  {e['date']} | {e['activity_name']} | {e['duration_minutes']}m | {e['source']} | {e['notes']}"
            for e in entries
        )
        or "  No entries logged."
    )

    oura_text = (
        "\n".join(
            f"  {o['date']} | Sleep:{o.get('sleep_score', '?')} Readiness:{o.get('readiness_score', '?')} "
            f"HRV:{o.get('avg_hrv', '?')} RHR:{o.get('resting_hr', '?')} Steps:{o.get('steps', '?')} "
            f"Deep:{o.get('deep_sleep_hours', '?')}h"
            for o in oura
        )
        or "  No Oura data."
    )

    energy_text = (
        "\n".join(
            f"  {d['date']} | Energy:{d.get('energy', '?')}/10 | "
            f"Morning:{d.get('morning_routine', '?')} Evening:{d.get('evening_routine', '?')} Water:{d.get('water', '?')}"
            for d in daily_logs
        )
        or "  No daily logs."
    )

    weight_text = (
        "\n".join(
            f"  {m['date']} | Weight:{m.get('weight', '?')}kg Waist:{m.get('waist', '?')}cm" for m in measurements
        )
        or "  No measurements."
    )

    system_prompt = (
        "You are a concise health & performance coach analyzing weekly experiment data. "
        "Provide actionable insights in 3-5 bullet points. Be direct, data-driven, encouraging but honest. "
        "Respond in English. Use markdown formatting."
    )

    scope = "FINAL EXPERIMENT SUMMARY" if is_final else f"WEEK {week_number} SUMMARY"

    user_prompt = f"""## {scope}: {exp["title"]}

**Description:** {exp["description"]}
**Week {week_number}/{exp["num_weeks"]}** ({week_start.isoformat()} → {week_end.isoformat()})
**Target:** {week_cfg["target_min"]}–{week_cfg["target_max"]} minutes

### Experiment Entries ({total_mins}m total)
{entries_text}

### Oura Ring Data
{oura_text}

### Daily Logs
{energy_text}

### Body Measurements
{weight_text}

{"Provide a final experiment summary: did the experiment achieve its goals? What worked, what didn't? What to do next?" if is_final else "Summarize this week's progress. What went well? What needs attention? Any patterns in the biometric data?"}"""

    try:
        summary = await call_llm(db, system_prompt, user_prompt)
    except Exception:
        logger.exception("Failed to generate experiment summary")
        raise

    # Store the summary
    await db.execute(
        """INSERT INTO experiment_summaries (experiment_id, week_number, summary)
           VALUES (?, ?, ?)
           ON CONFLICT(experiment_id, week_number) DO UPDATE SET summary = excluded.summary,
           created_at = datetime('now')""",
        (experiment_id, week_number, summary),
    )
    await db.commit()

    return summary


async def auto_generate_missing_summaries(db, experiment_id: int) -> list[int]:
    """Check for completed weeks without summaries and generate them. Returns list of generated week numbers."""
    if not await has_llm(db):
        return []

    # Cooldown: skip if we attempted this experiment within the last 5 minutes
    now = time.monotonic()
    last = _last_attempt.get(experiment_id, 0)
    if now - last < _COOLDOWN_SECONDS:
        return []
    _last_attempt[experiment_id] = now

    exp_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not exp_rows:
        return []
    exp = dict(exp_rows[0])

    today = date.today()
    start = date.fromisoformat(exp["start_date"])
    start_monday = start - timedelta(days=start.weekday())

    existing = await get_existing_summaries(db, experiment_id)
    generated = []

    for wn in range(1, exp["num_weeks"] + 1):
        week_end = start_monday + timedelta(weeks=wn - 1, days=6)
        # Only generate for completed weeks (week_end is in the past)
        if week_end >= today:
            break
        if wn in existing:
            continue
        try:
            await generate_week_summary(db, experiment_id, wn)
            generated.append(wn)
        except Exception:
            logger.exception("Failed to auto-generate summary for week %d", wn)
            break  # Don't spam LLM on repeated failures

    return generated
