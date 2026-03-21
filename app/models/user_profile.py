"""DB queries for user_profiles table."""


async def get_profile(db) -> dict | None:
    """Return the user profile row as a dict, or None if not yet created."""
    rows = await db.execute_fetchall("SELECT * FROM user_profiles WHERE id = 1")
    return dict(rows[0]) if rows else None


async def ensure_profile(db) -> dict:
    """Return existing profile or create an empty one."""
    profile = await get_profile(db)
    if profile:
        return profile
    await db.execute("INSERT INTO user_profiles (id) VALUES (1)")
    await db.commit()
    return await get_profile(db)


async def update_step1(
    db,
    sex: str,
    age: int | None,
    height_cm: float | None,
    weight_kg: float | None,
    family: str,
    habits_good: str,
    habits_bad: str,
) -> None:
    """Save Step 1 (About You) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            sex = ?, age = ?, height_cm = ?, weight_kg = ?,
            family = ?, habits_good = ?, habits_bad = ?,
            onboarding_step = MAX(onboarding_step, 1), updated_at = datetime('now')
        WHERE id = 1""",
        (sex or None, age, height_cm, weight_kg, family, habits_good, habits_bad),
    )
    await db.commit()


async def update_step2(db, ideal_day: str) -> None:
    """Save Step 2 (Ideal Day) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            ideal_day = ?, onboarding_step = MAX(onboarding_step, 2), updated_at = datetime('now')
        WHERE id = 1""",
        (ideal_day,),
    )
    await db.commit()


async def update_step3(db) -> None:
    """Mark Step 3 complete (goals saved directly to goals table)."""
    await ensure_profile(db)
    await db.execute(
        "UPDATE user_profiles SET onboarding_step = MAX(onboarding_step, 3), updated_at = datetime('now') WHERE id = 1"
    )
    await db.commit()


async def update_step4(db, training_routine: str, equipment: str, habits_build: str, habits_break: str) -> None:
    """Save Step 4 (Habits & Training) data."""
    await ensure_profile(db)
    await db.execute(
        """UPDATE user_profiles SET
            training_routine = ?, equipment = ?, habits_build = ?, habits_break = ?,
            onboarding_step = MAX(onboarding_step, 4), updated_at = datetime('now')
        WHERE id = 1""",
        (training_routine, equipment, habits_build, habits_break),
    )
    await db.commit()


async def update_step5(db) -> None:
    """Mark Step 5 complete (medical records saved to blood_markers/blood_results)."""
    await ensure_profile(db)
    await db.execute(
        "UPDATE user_profiles SET onboarding_step = MAX(onboarding_step, 5), updated_at = datetime('now') WHERE id = 1"
    )
    await db.commit()


async def save_enrichment(db, llm_summary: str | None, realistic_day: str | None) -> None:
    """Save LLM-generated enrichment data."""
    await db.execute(
        """UPDATE user_profiles SET
            llm_summary = COALESCE(?, llm_summary),
            realistic_day = COALESCE(?, realistic_day),
            onboarding_step = 6, updated_at = datetime('now')
        WHERE id = 1""",
        (llm_summary, realistic_day),
    )
    await db.commit()
