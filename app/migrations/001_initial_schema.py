"""Initial schema and seed data, frozen as they stood when this file was frozen.

New databases used to be built from the live SCHEMA and seeds in app/db.py,
which kept changing after this migration shipped. Each later migration already
carries its own SQL; this one now does too, so a fresh install is built by the
same steps forever. The schema snapshot test pins the result.
"""

import aiosqlite

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS daily_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    energy INTEGER,
    morning_routine TEXT DEFAULT 'pending' CHECK(morning_routine IN ('done','skipped','pending')),
    evening_routine TEXT DEFAULT 'pending' CHECK(evening_routine IN ('done','skipped','pending')),
    water TEXT DEFAULT 'pending' CHECK(water IN ('done','skipped','pending')),
    andy_body_status TEXT DEFAULT 'pending' CHECK(andy_body_status IN ('done','skipped','pending')),
    andy_body_desc TEXT DEFAULT '',
    andy_spirit_status TEXT DEFAULT 'pending' CHECK(andy_spirit_status IN ('done','skipped','pending')),
    andy_spirit_desc TEXT DEFAULT '',
    andy_account_status TEXT DEFAULT 'pending' CHECK(andy_account_status IN ('done','skipped','pending')),
    andy_account_desc TEXT DEFAULT '',
    andy_relations_status TEXT DEFAULT 'pending' CHECK(andy_relations_status IN ('done','skipped','pending')),
    andy_relations_desc TEXT DEFAULT '',
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    weight REAL,
    arm REAL,
    waist REAL,
    hips REAL,
    thighs REAL,
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS feniks_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    start_date TEXT NOT NULL DEFAULT '2026-02-05',
    target_days INTEGER NOT NULL DEFAULT 90,
    big_why TEXT DEFAULT ''
);""",
    """CREATE TABLE IF NOT EXISTS feniks_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    emotions TEXT DEFAULT '',
    triggers TEXT DEFAULT '',
    thoughts TEXT DEFAULT '',
    desired_feelings TEXT DEFAULT '',
    coping_strategies TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS feniks_pleasures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    pleasure_1 TEXT DEFAULT '',
    pleasure_2 TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS feniks_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_number INTEGER NOT NULL UNIQUE,
    week_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    notes TEXT DEFAULT '',
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);""",
    """CREATE TABLE IF NOT EXISTS pmo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'relapse' CHECK(event_type IN ('relapse','reset')),
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS feniks_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0, 1)),
    minutes INTEGER,
    edging INTEGER NOT NULL DEFAULT 0 CHECK(edging IN (0, 1)),
    note TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS feniks_bricks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    hook TEXT NOT NULL,
    craving INTEGER CHECK(craving BETWEEN 0 AND 10),
    story TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS oura_monthly (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL UNIQUE,
    sleep_score REAL,
    readiness REAL,
    activity REAL,
    steps INTEGER,
    sleep_duration REAL,
    deep_sleep REAL,
    rem_sleep REAL,
    rhr REAL,
    lowest_hr REAL,
    hrv REAL,
    cardiovascular_age INTEGER,
    stress_normal INTEGER,
    stress_stressful INTEGER,
    stress_restored INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS blood_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    ref_low REAL,
    ref_high REAL,
    display_order INTEGER DEFAULT 0
);""",
    """CREATE TABLE IF NOT EXISTS blood_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id INTEGER NOT NULL REFERENCES blood_markers(id),
    date TEXT NOT NULL,
    value REAL NOT NULL,
    value_text TEXT DEFAULT '',
    flag TEXT DEFAULT '' CHECK(flag IN ('','H','L')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(marker_id, date)
);""",
    """CREATE TABLE IF NOT EXISTS life_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    planning INTEGER,
    spirituality INTEGER,
    health INTEGER,
    work INTEGER,
    social INTEGER,
    growth INTEGER,
    relaxation INTEGER,
    family INTEGER,
    power_level REAL,
    weight REAL,
    waist REAL,
    pmo_status TEXT DEFAULT '',
    energy_avg REAL,
    linkedin_followers INTEGER,
    youtube_views INTEGER,
    revenue REAL,
    diagnostic TEXT DEFAULT '',
    priorities TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS goal_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);""",
    """CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL REFERENCES goal_areas(id),
    horizon TEXT NOT NULL CHECK(horizon IN ('1yr','3yr','10yr')),
    content TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','completed','abandoned')),
    start_date TEXT,
    end_date TEXT,
    completed_at TEXT,
    parent_goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    display_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS goal_reps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    content TEXT NOT NULL CHECK(length(trim(content)) BETWEEN 1 AND 2000),
    period TEXT NOT NULL CHECK(period IN ('day','week','month','quarter','year')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','carried','skipped')),
    completed_at TEXT,
    carried_from_id INTEGER REFERENCES goal_reps(id) ON DELETE SET NULL,
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(period_start <= due_date AND due_date <= period_end),
    CHECK((status = 'completed' AND completed_at IS NOT NULL) OR
          (status != 'completed' AND completed_at IS NULL))
);""",
    """CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','error')),
    message TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    start_date TEXT NOT NULL,
    num_weeks INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','abandoned')),
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS experiment_activity_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#3b82f6',
    display_order INTEGER DEFAULT 0,
    source_match TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'duration' CHECK(kind IN ('duration','count','boolean','scale')),
    target_value INTEGER NOT NULL DEFAULT 0,
    target_period TEXT NOT NULL DEFAULT 'week' CHECK(target_period IN ('day','week','total'))
);""",
    """CREATE TABLE IF NOT EXISTS experiment_weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    label TEXT DEFAULT '',
    target_min INTEGER DEFAULT 0,
    target_max INTEGER DEFAULT 0,
    UNIQUE(experiment_id, week_number)
);""",
    """CREATE TABLE IF NOT EXISTS experiment_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    activity_type_id INTEGER NOT NULL REFERENCES experiment_activity_types(id) ON DELETE CASCADE,
    value INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS experiment_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(experiment_id, week_number)
);""",
    """CREATE TABLE IF NOT EXISTS oura_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    activity TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    calories REAL,
    distance_meters REAL,
    intensity TEXT DEFAULT '',
    start_datetime TEXT,
    end_datetime TEXT,
    oura_id TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS integrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    client_secret_enc TEXT NOT NULL,
    access_token_enc TEXT DEFAULT '',
    refresh_token_enc TEXT DEFAULT '',
    token_expires_at TEXT DEFAULT '',
    scopes TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'configured' CHECK(status IN ('configured','connected','error')),
    last_sync_at TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS oura_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    sleep_score INTEGER,
    readiness_score INTEGER,
    activity_score INTEGER,
    steps INTEGER,
    sleep_duration_hours REAL,
    deep_sleep_hours REAL,
    rem_sleep_hours REAL,
    resting_hr REAL,
    lowest_hr REAL,
    avg_hrv REAL,
    stress_high INTEGER,
    stress_medium INTEGER,
    stress_low INTEGER,
    stress_rest INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS llm_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    model TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS training_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    section TEXT NOT NULL,
    target_sets INTEGER,
    target_reps TEXT,
    notes TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);""",
    """CREATE TABLE IF NOT EXISTS training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS training_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES training_exercises(id),
    set_number INTEGER NOT NULL,
    reps INTEGER,
    weight REAL,
    notes TEXT DEFAULT ''
);""",
    """CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret TEXT DEFAULT '',
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);""",
    """CREATE TABLE IF NOT EXISTS daily_briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);""",
)

_SEEDS = (
    """INSERT OR IGNORE INTO feniks_config (id, start_date, target_days, big_why)
VALUES (1, '2026-01-01', 90, '');""",
    """INSERT OR IGNORE INTO goal_areas (name, icon, display_order) VALUES
('Life Planning', '📋', 1),
('Spirituality', '✨', 2),
('Health', '💪', 3),
('Work', '💼', 4),
('Social Life', '👥', 5),
('Growth', '🎓', 6),
('Relaxation', '🎮', 7),
('Family', '👨‍👩‍👧‍👦', 8);""",
    """INSERT OR IGNORE INTO training_exercises (id, name, section, target_sets, target_reps, notes, display_order) VALUES
(1, 'Jump Rope', 'Warmup', 1, '3 min', '', 1),
(2, 'Halo (KB)', 'Warmup', 1, '10/side', 'Shoulder mobility', 2),
(3, 'Goblet Squat (KB)', 'Warmup', 1, '1 min', 'Deep squat hold', 3),
(4, 'Dead Hang', 'Warmup', 1, '60 sec', '', 4),
(5, 'Arm Circles', 'Warmup', 1, '30 sec', '', 5),
(6, 'Double KB Swing', 'Core', 5, '10-15', 'Explosive hip extension', 6),
(7, 'Double KB Front Squat', 'Core', 5, '8-10', 'Elbows tight, chest up', 7),
(8, 'KB Press', 'Core', 5, '8-10', 'Seated or standing', 8),
(9, 'Gorilla Row', 'Core', 5, '10/side', 'Wide stance, flat back', 9),
(10, 'Pull-ups', 'Core', 3, 'MAX', '', 10),
(11, 'Ab Wheel Rollout', 'Core', 3, 'MAX', 'From knees, cat-back', 11),
(12, 'DB Curl', 'Core', 3, '10-12', '', 12),
(13, 'DB Lateral Raise', 'Core', 3, '10-12', '', 13),
(14, 'Boxing Bag', 'Cardio', 5, '2 min', '', 14),
(15, 'Jump Rope HIIT', 'Cardio', 5, '3 min', '', 15),
(16, 'KB Snatch', 'Cardio', 5, '10/side', '', 16),
(17, 'Full Body Stretch', 'Stretching', 1, '10 min', '', 17),
(18, 'Hip Flexor Stretch', 'Stretching', 1, '5 min', '', 18),
(19, 'Shoulder Mobility', 'Stretching', 1, '5 min', '', 19);""",
    """INSERT OR IGNORE INTO app_settings (key, value) VALUES
('backup_enabled', '1'),
('backup_interval_hours', '24'),
('backup_max_copies', '7'),
('oura_sync_enabled', '0'),
('oura_sync_interval_hours', '6'),
('briefing_enabled', '0');""",
)


async def up(db: aiosqlite.Connection) -> None:
    # execute(), never executescript(): executescript commits on its own and
    # would escape the runner's transaction.
    for statement in (*_SCHEMA, *_SEEDS):
        await db.execute(statement)
