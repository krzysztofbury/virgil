"""LLM enrichment logic for onboarding — runs after user confirms Step 6."""

import json
import logging
from datetime import date

import litellm

from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL
from app.services.goal_data import create_goal

logger = logging.getLogger(__name__)

MAX_GOAL_EXPANSIONS = 20


# Four independent purchases, run in this order because each later one reads the
# profile summary the first may have written. Each publishes on its own, so a
# retry after a partial failure re-buys only what is still missing.
ENRICHMENT_STEPS = ("profile_summary", "realistic_day", "goal_expansion", "habit_experiment")
ENRICHMENT_STEP_LABELS = {
    "profile_summary": "Profile summary",
    "realistic_day": "A realistic version of your day",
    "goal_expansion": "One-year and three-year milestones",
    "habit_experiment": "A first experiment",
}


def enrichment_step_key(step: str) -> str:
    from app.services.llm_jobs import paid_llm_job_key

    if step not in ENRICHMENT_STEPS:
        raise ValueError("Unknown onboarding enrichment step")
    return paid_llm_job_key("onboarding_enrichment", step)


async def enrichment_applies(db, step: str, profile: dict) -> bool:
    """Whether this step has anything to work with. No input, no purchase."""
    if step == "profile_summary":
        return bool(profile.get("sex") or profile.get("age") or profile.get("family"))
    if step == "realistic_day":
        return bool(profile.get("ideal_day"))
    if step == "goal_expansion":
        rows = await db.execute_fetchall("SELECT 1 FROM goals WHERE horizon = '10yr' LIMIT 1")
        return bool(rows)
    if step == "habit_experiment":
        return bool(profile.get("habits_break"))
    raise ValueError("Unknown onboarding enrichment step")


async def enrichment_progress(db) -> list[dict]:
    """Per-step state for the onboarding screen: done, waiting, or not needed."""
    from app.models.user_profile import get_profile
    from app.services.llm_jobs import llm_result_published

    profile = await get_profile(db) or {}
    progress = []
    for step in ENRICHMENT_STEPS:
        applies = await enrichment_applies(db, step, profile)
        done = await llm_result_published(db, "onboarding_enrichment", enrichment_step_key(step))
        progress.append(
            {
                "step": step,
                "label": ENRICHMENT_STEP_LABELS[step],
                "state": "done" if done else ("waiting" if applies else "not_needed"),
            }
        )
    return progress


def apply_feniks_trigger_words(profile: dict) -> bool:
    """Whether the stated habits ask for the No Porn module. No LLM involved."""
    habits = (profile.get("habits_bad") or "") + " " + (profile.get("habits_break") or "")
    return any(word in habits.lower() for word in ("porn", "pmo", "masturbat", "nofap", "porno"))


async def enrichment_units(db, profile: dict) -> list[dict]:
    """Build the produce/publish pair for every step that still has work to do.

    Steps that do not apply are absent. The caller checks the ledger, so a step
    that already published is skipped without a second charge.
    """
    from app.models.user_profile import save_enrichment

    units = []

    async def summary_publish(text: str) -> dict:
        await save_enrichment(db, text, None, commit=False)
        return {"chars": len(text)}

    async def day_publish(text: str) -> dict:
        await save_enrichment(db, None, text, commit=False)
        return {"chars": len(text)}

    async def goals_publish(items: list) -> dict:
        return {"goals": await _save_goal_levels(db, items)}

    async def experiment_publish(exp: dict | None) -> dict:
        if not exp:
            return {"experiment_id": None}
        return {"experiment_id": await create_suggested_experiment(db, exp, commit=False)}

    builders = {
        "profile_summary": (lambda: _generate_profile_summary(profile), summary_publish),
        "realistic_day": (lambda: _generate_realistic_day(profile, profile.get("llm_summary")), day_publish),
        "goal_expansion": (lambda: _expand_goals(db, profile.get("llm_summary")), goals_publish),
        "habit_experiment": (lambda: _analyze_habits(db, profile, profile.get("llm_summary")), experiment_publish),
    }
    for step in ENRICHMENT_STEPS:
        if not await enrichment_applies(db, step, profile):
            continue
        produce, publish = builders[step]
        units.append({"step": step, "key": enrichment_step_key(step), "produce": produce, "publish": publish})
    return units


async def _llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> str:
    """Internal LLM call using env-var provider."""
    response = await litellm.acompletion(
        model=INTERNAL_LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        api_key=INTERNAL_LLM_KEY,
        max_tokens=max_tokens,
        timeout=90.0,
    )
    if not response.choices:
        raise ValueError("LLM returned empty choices array")
    content = response.choices[0].message.content
    if content is None:
        raise ValueError("LLM returned null content")
    return content


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


async def _generate_profile_summary(profile: dict) -> str:
    """Generate a concise profile paragraph for use as LLM context."""
    parts = []
    if profile.get("sex"):
        parts.append(f"Sex: {profile['sex']}")
    if profile.get("age"):
        parts.append(f"Age: {profile['age']}")
    if profile.get("height_cm"):
        parts.append(f"Height: {profile['height_cm']}cm")
    if profile.get("weight_kg"):
        parts.append(f"Weight: {profile['weight_kg']}kg")
    if profile.get("family"):
        parts.append(f"Family: {profile['family']}")
    if profile.get("habits_good"):
        parts.append(f"Good habits: {profile['habits_good']}")
    if profile.get("habits_bad"):
        parts.append(f"Struggles with: {profile['habits_bad']}")

    return await _llm_call(
        "You are a personal development assistant. Write a concise profile summary (2-3 sentences) "
        "that captures the key facts about this person. This will be used as context for future AI interactions. "
        "Write in the same language the user used in their input.",
        "\n".join(parts),
        max_tokens=256,
    )


async def _generate_realistic_day(profile: dict, llm_summary: str | None) -> str:
    """Generate a realistic daily schedule based on the user's ideal day and profile."""
    context_parts = []
    if llm_summary:
        context_parts.append(f"User profile: {llm_summary}")
    if profile.get("family"):
        context_parts.append(f"Family: {profile['family']}")
    if profile.get("training_routine"):
        context_parts.append(f"Training: {profile['training_routine']}")

    return await _llm_call(
        "You are a personal development assistant creating a realistic daily schedule. "
        "The user has provided their ideal day. Create a realistic version that accounts for "
        "their real obligations (family, work, energy levels). "
        "Format as time-blocked phases with practical notes. "
        "Be honest about constraints — if they have young kids, morning routine needs to be flexible. "
        "Write in the same language the user used in their ideal day description.",
        f"User context:\n{chr(10).join(context_parts)}\n\nIdeal day:\n{profile['ideal_day']}",
        max_tokens=2048,
    )


async def _expand_goals(db, llm_summary: str | None) -> list:
    """For each Level 3 (10yr) goal, generate Level 2 (3yr, ~35%) and Level 1 (1yr, ~10%).

    Reads and buys only. _save_goal_levels performs the write, so the caller can
    commit it together with the publication marker.
    """
    rows = await db.execute_fetchall(
        """SELECT g.id, g.area_id, g.content, ga.name as area_name
           FROM goals g JOIN goal_areas ga ON g.area_id = ga.id
           WHERE g.horizon = '10yr'"""
    )
    if not rows:
        return []

    goals_text = "\n".join(f"- goal_id={row['id']} | {row['area_name']}: {row['content']}" for row in rows)

    context = f"User profile: {llm_summary}\n\n" if llm_summary else ""

    raw = await _llm_call(
        "You are a goal-setting assistant. For each end goal (Level 3, 10-year vision), "
        "create two milestone levels:\n"
        "- Level 2 (3-year, ~35% of the end goal): A meaningful intermediate milestone.\n"
        "- Level 1 (1-year, ~10% of the end goal): A concrete, achievable first step.\n\n"
        "Return ONLY valid JSON, no markdown fences. Format:\n"
        '[{"goal_id": 123, "level2": "...", "level1": "..."}]\n'
        "Write goals in the same language as the input.",
        f"{context}End goals (Level 3):\n{goals_text}",
        max_tokens=2048,
    )

    # Parse response.
    cleaned = _strip_fences(raw)

    try:
        goal_levels = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse goal expansion JSON")
        return []

    if not isinstance(goal_levels, list):
        return []
    return goal_levels[:MAX_GOAL_EXPANSIONS]


async def _save_goal_levels(db, goal_levels: list) -> int:
    """Store the expanded milestones. The caller owns the transaction."""
    written = 0
    for item in goal_levels:
        if not isinstance(item, dict):
            continue
        goal_id = item.get("goal_id")
        if not isinstance(goal_id, int):
            continue
        parent_rows = await db.execute_fetchall("SELECT id, area_id FROM goals WHERE id = ?", (goal_id,))
        if not parent_rows:
            continue
        parent = parent_rows[0]

        for horizon, key in [("3yr", "level2"), ("1yr", "level1")]:
            content = item.get(key, "")
            if content:
                _, created = await create_goal(
                    db,
                    area_id=parent["area_id"],
                    horizon=horizon,
                    content=str(content),
                    display_order=1,
                    source="onboarding",
                    source_ref=f"goal-expansion:{goal_id}:{horizon}",
                    parent_goal_id=goal_id,
                )
                written += int(created)
    return written


async def _analyze_habits(db, profile: dict, llm_summary: str | None) -> dict | None:
    """Buy one replacement experiment for the most costly stated bad habit.

    The Feniks trigger words moved to the confirm route: matching them is a
    string comparison, not a purchase, and it must not wait on a worker.
    """
    if not profile.get("habits_break"):
        return None

    context = f"User profile: {llm_summary}\n\n" if llm_summary else ""

    raw = await _llm_call(
        "You are a habit coach. Pick the ONE most impactful bad habit from the list and suggest "
        "a replacement experiment. Return ONLY valid JSON:\n"
        '{"title": "...", "description": "...", "num_weeks": 4-8, '
        '"weekly_target_min": minutes_per_week, "weekly_target_max": minutes_per_week}\n'
        "The experiment should be realistic and specific. Write in the same language as the input.",
        f"{context}Bad habits to break:\n{profile['habits_break']}\n\n"
        f"Good habits to build:\n{profile.get('habits_build', 'none mentioned')}",
        max_tokens=512,
    )

    cleaned = _strip_fences(raw)

    try:
        exp = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse experiment suggestion JSON")
        return None

    if not isinstance(exp, dict) or not exp.get("title"):
        return None
    return exp


def _coerce_minutes(value, default: int) -> int:
    """LLM output → bounded weekly minutes (0..10080, one week)."""
    try:
        minutes = int(value)
    except (ValueError, TypeError):
        return default
    return max(0, min(10080, minutes))


async def create_suggested_experiment(db, exp: dict, *, commit: bool = True) -> int:
    """Persist an LLM-suggested experiment using the real schema.

    Weekly targets live in experiment_weeks (one row per week), NOT on the
    experiments table, and the UI cannot log entries without at least one
    activity type — so both are created alongside the experiment.
    Returns the new experiment id.
    """
    assert exp.get("title"), "Experiment suggestion must have a title"

    today = date.today().isoformat()
    num_weeks = min(12, max(2, exp.get("num_weeks") if isinstance(exp.get("num_weeks"), int) else 4))
    target_min = _coerce_minutes(exp.get("weekly_target_min"), default=60)
    target_max = max(target_min, _coerce_minutes(exp.get("weekly_target_max"), default=120))

    cursor = await db.execute(
        """INSERT INTO experiments (title, description, start_date, num_weeks, status)
           VALUES (?, ?, ?, ?, 'active')""",
        (str(exp["title"])[:200], str(exp.get("description", ""))[:2000], today, num_weeks),
    )
    experiment_id = cursor.lastrowid

    await db.execute(
        """INSERT INTO experiment_activity_types (experiment_id, name, color, kind, display_order)
           VALUES (?, ?, '#22c55e', 'duration', 1)""",
        (experiment_id, str(exp["title"])[:100]),
    )

    for week_number in range(1, num_weeks + 1):
        await db.execute(
            """INSERT INTO experiment_weeks (experiment_id, week_number, target_min, target_max)
               VALUES (?, ?, ?, ?)""",
            (experiment_id, week_number, target_min, target_max),
        )

    if commit:
        await db.commit()
    logger.info("Onboarding experiment created: id=%d weeks=%d", experiment_id, num_weeks)
    return experiment_id
