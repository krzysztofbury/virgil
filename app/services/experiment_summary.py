import logging
from datetime import date, timedelta

from app.services.llm import call_llm, llm_available

SUMMARY_JOB_KIND = "experiment_summary"
SUMMARY_MAX_CHARS = 8000
# One tick looks at this many due weeks before giving up, so the scan stays
# bounded no matter how long an experiment is.
SUMMARY_ENQUEUE_SCAN_MAX = 12

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


async def build_week_summary(db, experiment_id: int, week_number: int) -> str:
    """Collect cross-board metrics for one experiment week and buy a summary.

    Reads only, so a failure here leaves nothing to roll back.
    """
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
        """SELECT ee.date, ee.value, ee.notes, ee.source,
                  eat.name as activity_name, eat.kind
           FROM experiment_entries ee
           JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id
           WHERE ee.experiment_id = ? AND ee.date >= ? AND ee.date <= ?
           ORDER BY ee.date""",
        (experiment_id, week_start.isoformat(), week_end.isoformat()),
    )
    entries = [dict(e) for e in entries]

    # Metric definitions give the LLM the target context per kind
    metrics = [
        dict(m)
        for m in await db.execute_fetchall(
            "SELECT name, kind, target_value, target_period FROM experiment_activity_types "
            "WHERE experiment_id = ? ORDER BY display_order",
            (experiment_id,),
        )
    ]

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

    # Build the prompt — render each entry value in its metric's unit
    def _fmt_value(kind: str, value: int) -> str:
        if kind == "duration":
            return f"{value}m"
        if kind == "count":
            return f"+{value}"
        if kind == "boolean":
            return "yes" if value == 1 else "no"
        return f"{value}/10"

    total_mins = sum(e["value"] for e in entries if e["kind"] == "duration")
    entries_text = (
        "\n".join(
            f"  {e['date']} | {e['activity_name']} | {_fmt_value(e['kind'], e['value'])} | {e['source']} | {e['notes']}"
            for e in entries
        )
        or "  No entries logged."
    )

    metrics_text = "\n".join(
        f"  {m['name']} ({m['kind']})"
        + (f" — target {m['target_value']}/{m['target_period']}" if m["target_value"] else "")
        for m in metrics
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

    has_duration = any(m["kind"] == "duration" for m in metrics)
    weekly_target_line = (
        f"**Weekly minutes target:** {week_cfg['target_min']}–{week_cfg['target_max']} minutes\n"
        if has_duration
        else ""
    )
    entries_heading = f"### Experiment Entries ({total_mins}m total)" if has_duration else "### Experiment Entries"

    user_prompt = f"""## {scope}: {exp["title"]}

**Description:** {exp["description"]}
**Week {week_number}/{exp["num_weeks"]}** ({week_start.isoformat()} → {week_end.isoformat()})
{weekly_target_line}
### Tracked Metrics
{metrics_text or "  (none)"}

{entries_heading}
{entries_text}

### Oura Ring Data
{oura_text}

### Daily Logs
{energy_text}

### Body Measurements
{weight_text}

{"Provide a final experiment summary: did the experiment achieve its goals? What worked, what didn't? What to do next?" if is_final else "Summarize this week's progress. What went well? What needs attention? Any patterns in the biometric data?"}"""

    return await call_llm(db, system_prompt, user_prompt)


async def save_week_summary(db, experiment_id: int, week_number: int, summary: str) -> int:
    """Store one week's summary. The caller owns the transaction, because the
    summary and its publication marker have to commit together."""
    text = summary.strip()[:SUMMARY_MAX_CHARS]
    if not text:
        raise ValueError("The provider returned an empty summary")
    await db.execute(
        """INSERT INTO experiment_summaries (experiment_id, week_number, summary)
           VALUES (?, ?, ?)
           ON CONFLICT(experiment_id, week_number) DO UPDATE SET summary = excluded.summary,
           created_at = datetime('now')""",
        (experiment_id, week_number, text),
    )
    return len(text)


async def due_summary_weeks(db, experiment_id: int) -> list[int]:
    """Completed weeks of one experiment that still have no summary."""
    exp_rows = await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
    if not exp_rows:
        return []
    exp = dict(exp_rows[0])
    start = date.fromisoformat(exp["start_date"])
    start_monday = start - timedelta(days=start.weekday())
    today = date.today()
    existing = await get_existing_summaries(db, experiment_id)

    due = []
    for week_number in range(1, exp["num_weeks"] + 1):
        week_end = start_monday + timedelta(weeks=week_number - 1, days=6)
        if week_end >= today:
            break
        if week_number not in existing:
            due.append(week_number)
    return due


async def enqueue_due_summary(db, experiment_id: int) -> int | None:
    """Queue at most one missing week summary for this experiment.

    One at a time on purpose: migration 029 allows a single queued paid job per
    kind, and a completed experiment can be missing a dozen weeks at once. The
    idempotency key is the week itself, so the queue - not a process-local
    cooldown - is what stops the same week being bought twice. The old cooldown
    lived in a module dict, which neither survived a restart nor separated one
    user from another.
    """
    from app.services.job_producers import ActiveWorkloadConflictError
    from app.services.llm_jobs import enqueue_paid_llm_job, paid_llm_job_key

    if not await has_llm(db):
        return None
    weeks = await due_summary_weeks(db, experiment_id)

    # Walk the due weeks rather than taking the first. A week that failed for
    # good keeps its idempotency key, so re-enqueueing it returns the old job
    # with created=False - and because nothing ever wrote its summary, it stays
    # due forever. Stopping at weeks[0] made that one week own the queue and
    # left every week behind it unbuyable for the life of the experiment.
    for week_number in weeks[:SUMMARY_ENQUEUE_SCAN_MAX]:
        try:
            result = await enqueue_paid_llm_job(
                db,
                SUMMARY_JOB_KIND,
                {"experiment_id": experiment_id, "week_number": week_number, "trigger": "scheduled"},
                idempotency_key=paid_llm_job_key(SUMMARY_JOB_KIND, str(experiment_id), str(week_number), "scheduled"),
            )
        except ActiveWorkloadConflictError:
            # Another summary already holds the single queued slot for this kind.
            return None
        if result.created:
            return result.job_id
    return None
