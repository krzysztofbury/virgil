"""LLM enrichment logic for onboarding — runs after user confirms Step 6."""

import json
import logging
from datetime import date

import litellm

from app.config import INTERNAL_LLM_KEY, INTERNAL_LLM_MODEL
from app.db import set_setting
from app.models.user_profile import save_enrichment

logger = logging.getLogger(__name__)


async def run_enrichment(db, profile: dict) -> None:
    """Run all applicable LLM enrichment steps. Each is independent and optional."""
    if not INTERNAL_LLM_KEY:
        logger.warning("No VIRGIL_INTERNAL_LLM_KEY set — skipping LLM enrichment")
        return

    llm_summary = None
    realistic_day = None

    # 1. Profile summary (if Step 1 data exists).
    if profile.get("sex") or profile.get("age") or profile.get("family"):
        try:
            llm_summary = await _generate_profile_summary(profile)
        except Exception:
            logger.exception("Failed to generate profile summary")

    # 2. Realistic day (if Step 2 data exists).
    if profile.get("ideal_day"):
        try:
            realistic_day = await _generate_realistic_day(profile, llm_summary)
        except Exception:
            logger.exception("Failed to generate realistic day")

    # Save profile enrichment.
    await save_enrichment(db, llm_summary, realistic_day)

    # 3. Goal expansion (if goals exist in DB).
    try:
        await _expand_goals(db, llm_summary)
    except Exception:
        logger.exception("Failed to expand goals")

    # 4. Habit analysis (if Step 4 data exists).
    if profile.get("training_routine") or profile.get("habits_break"):
        try:
            await _analyze_habits(db, profile, llm_summary)
        except Exception:
            logger.exception("Failed to analyze habits")


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
    return response.choices[0].message.content


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


async def _expand_goals(db, llm_summary: str | None) -> None:
    """For each Level 3 (10yr) goal, generate Level 2 (3yr, ~35%) and Level 1 (1yr, ~10%)."""
    rows = await db.execute_fetchall(
        """SELECT g.id, g.area_id, g.content, ga.name as area_name
           FROM goals g JOIN goal_areas ga ON g.area_id = ga.id
           WHERE g.horizon = '10yr'"""
    )
    if not rows:
        return

    goals_text = "\n".join(f"- {row['area_name']}: {row['content']}" for row in rows)

    context = f"User profile: {llm_summary}\n\n" if llm_summary else ""

    raw = await _llm_call(
        "You are a goal-setting assistant. For each end goal (Level 3, 10-year vision), "
        "create two milestone levels:\n"
        "- Level 2 (3-year, ~35% of the end goal): A meaningful intermediate milestone.\n"
        "- Level 1 (1-year, ~10% of the end goal): A concrete, achievable first step.\n\n"
        "Return ONLY valid JSON, no markdown fences. Format:\n"
        '[{"area_name": "...", "level2": "...", "level1": "..."}]\n'
        "Write goals in the same language as the input.",
        f"{context}End goals (Level 3):\n{goals_text}",
        max_tokens=2048,
    )

    # Parse response.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        goal_levels = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse goal expansion JSON")
        return

    if not isinstance(goal_levels, list):
        return

    for item in goal_levels:
        area_name = item.get("area_name", "")
        area_row = await db.execute_fetchall("SELECT id FROM goal_areas WHERE name = ?", (area_name,))
        if not area_row:
            continue
        area_id = area_row[0]["id"]

        for horizon, key in [("3yr", "level2"), ("1yr", "level1")]:
            content = item.get(key, "")
            if content:
                await db.execute(
                    """INSERT INTO goals (area_id, horizon, content, display_order)
                       VALUES (?, ?, ?, 1)
                       ON CONFLICT DO NOTHING""",
                    (area_id, horizon, content),
                )

    await db.commit()


async def _analyze_habits(db, profile: dict, llm_summary: str | None) -> None:
    """Check for Feniks trigger and suggest one experiment."""
    habits_bad = (profile.get("habits_bad") or "") + " " + (profile.get("habits_break") or "")

    # Check for Feniks trigger words.
    feniks_keywords = ["porn", "pmo", "masturbat", "nofap", "porno"]
    if any(kw in habits_bad.lower() for kw in feniks_keywords):
        await set_setting(db, "feature_feniks", "1")
        logger.info("Feniks feature auto-enabled based on onboarding habits")

    # Suggest one experiment to replace a bad habit.
    if not profile.get("habits_break"):
        return

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

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        exp = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Could not parse experiment suggestion JSON")
        return

    if not isinstance(exp, dict) or not exp.get("title"):
        return

    today = date.today().isoformat()
    num_weeks = min(12, max(2, exp.get("num_weeks", 4)))

    await db.execute(
        """INSERT INTO experiments (title, description, start_date, num_weeks,
           weekly_target_min, weekly_target_max, status)
           VALUES (?, ?, ?, ?, ?, ?, 'active')""",
        (
            exp["title"],
            exp.get("description", ""),
            today,
            num_weeks,
            exp.get("weekly_target_min", 60),
            exp.get("weekly_target_max", 120),
        ),
    )
    await db.commit()
