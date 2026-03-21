"""Create user_profiles table and onboarding_completed setting."""


async def up(db):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sex TEXT,
            age INTEGER,
            height_cm REAL,
            weight_kg REAL,
            family TEXT,
            habits_good TEXT,
            habits_bad TEXT,
            ideal_day TEXT,
            realistic_day TEXT,
            training_routine TEXT,
            equipment TEXT,
            habits_build TEXT,
            habits_break TEXT,
            llm_summary TEXT,
            onboarding_step INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Seed onboarding_completed=0 so auth middleware knows to redirect.
    await db.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES ('onboarding_completed', '0')")
