"""Training overhaul: add duration column, translate exercises to English, add Stretching section."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    # 1. Add duration column to training_entries
    await db.execute("ALTER TABLE training_entries ADD COLUMN duration REAL")

    # 2. Rename sections
    await db.execute("UPDATE training_exercises SET section = 'Warmup' WHERE section = 'Rozgrzewka'")
    await db.execute("UPDATE training_exercises SET section = 'Core' WHERE section IN ('Main Circuit', 'Core')")

    # 3. Translate exercise names and notes
    translations = [
        ("Skakanka", "Jump Rope", ""),
        ("Halo (KB 8kg)", "Halo (KB)", "Shoulder mobility"),
        ("Prying Goblet Squat (KB 16kg)", "Goblet Squat (KB)", "Deep squat hold"),
        ("Zwis na drążku", "Dead Hang", ""),
        ("Kneeling Press (KB 8kg)", "Arm Circles", ""),
        ("Z-Press (Double)", "KB Press", "Seated or standing"),
        ("Ab Wheel", "Ab Wheel Rollout", "From knees, cat-back"),
        ("Worek (Boxing)", "Boxing Bag", ""),
    ]
    for old_name, new_name, new_notes in translations:
        if new_notes:
            await db.execute(
                "UPDATE training_exercises SET name = ?, notes = ? WHERE name = ?",
                (new_name, new_notes, old_name),
            )
        else:
            await db.execute(
                "UPDATE training_exercises SET name = ? WHERE name = ?",
                (new_name, old_name),
            )

    # 4. Add missing exercises for complete 4-section protocol
    # Check max display_order
    row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) as mx FROM training_exercises")
    order = row[0]["mx"] + 1

    new_exercises = [
        # Core section additions
        ("Pull-ups", "Core", 3, "MAX", "", order),
        ("DB Curl", "Core", 3, "10-12", "", order + 1),
        ("DB Lateral Raise", "Core", 3, "10-12", "", order + 2),
        # Cardio additions
        ("Jump Rope HIIT", "Cardio", 5, "3 min", "", order + 3),
        ("KB Snatch", "Cardio", 5, "10/side", "", order + 4),
        # Stretching section (new)
        ("Full Body Stretch", "Stretching", 1, "10 min", "", order + 5),
        ("Hip Flexor Stretch", "Stretching", 1, "5 min", "", order + 6),
        ("Shoulder Mobility", "Stretching", 1, "5 min", "", order + 7),
    ]
    for name, section, sets, reps, notes, disp in new_exercises:
        await db.execute(
            "INSERT INTO training_exercises (name, section, target_sets, target_reps, notes, display_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, section, sets, reps, notes, disp),
        )
