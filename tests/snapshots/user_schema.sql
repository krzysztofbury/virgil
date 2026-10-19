-- index idx_exercise_aliases_library
CREATE INDEX idx_exercise_aliases_library ON exercise_aliases(library_id);

-- index idx_experiment_metric_name
CREATE UNIQUE INDEX idx_experiment_metric_name ON experiment_activity_types(experiment_id, name COLLATE NOCASE);

-- index idx_experiments_goal_id
CREATE INDEX idx_experiments_goal_id ON experiments(goal_id);

-- index idx_feniks_bricks_habit_date
CREATE INDEX idx_feniks_bricks_habit_date ON feniks_bricks(habit_id, date);

-- index idx_goal_reps_carried_from
CREATE UNIQUE INDEX idx_goal_reps_carried_from ON goal_reps(carried_from_id) WHERE carried_from_id IS NOT NULL;

-- index idx_goal_reps_pending
CREATE INDEX idx_goal_reps_pending ON goal_reps(status, due_date, id);

-- index idx_goal_reps_source_ref
CREATE UNIQUE INDEX idx_goal_reps_source_ref ON goal_reps(source, source_ref) WHERE source_ref != '';

-- index idx_goals_source_ref
CREATE UNIQUE INDEX idx_goals_source_ref ON goals(source, source_ref) WHERE source_ref != '';

-- index idx_jobs_claimable
CREATE INDEX idx_jobs_claimable ON jobs(run_after, id) WHERE status = 'queued';

-- index idx_jobs_idempotency
CREATE UNIQUE INDEX idx_jobs_idempotency ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- index idx_jobs_queued_paid_llm_kind
CREATE UNIQUE INDEX idx_jobs_queued_paid_llm_kind ON jobs(kind) WHERE status = 'queued' AND kind IN ('andy_generation', 'experiment_summary', 'medical_import', 'morning_briefing', 'onboarding_enrichment', 'wod_parse');

-- index idx_jobs_queued_workload_kind
CREATE UNIQUE INDEX idx_jobs_queued_workload_kind ON jobs(kind) WHERE status = 'queued' AND kind IN ('backup', 'markdown_export', 'oura_sync');

-- index idx_jobs_recent
CREATE INDEX idx_jobs_recent ON jobs(created_at DESC, id DESC);

-- index idx_jobs_stale
CREATE INDEX idx_jobs_stale ON jobs(locked_at, id) WHERE status = 'running';

-- index idx_library_tags_tag
CREATE INDEX idx_library_tags_tag ON exercise_library_tags(tag);

-- index idx_pmo_events_habit_date
CREATE INDEX idx_pmo_events_habit_date ON pmo_events(habit_id, date);

-- index idx_sessions_capture_token
CREATE UNIQUE INDEX idx_sessions_capture_token ON training_sessions(capture_token) WHERE capture_token IS NOT NULL;

-- index idx_training_entries_session_id
CREATE INDEX idx_training_entries_session_id ON training_entries(session_id);

-- index idx_training_exercises_name_nocase
CREATE UNIQUE INDEX idx_training_exercises_name_nocase ON training_exercises(name COLLATE NOCASE);

-- index idx_training_sessions_date_id
CREATE INDEX idx_training_sessions_date_id ON training_sessions(date, id DESC);

-- index uq_entry_source
CREATE UNIQUE INDEX uq_entry_source ON experiment_entries(experiment_id, source, source_ref) WHERE source != 'manual';

-- table app_settings
CREATE TABLE app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

-- table auth_users
CREATE TABLE auth_users (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret TEXT DEFAULT '',
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- table blood_markers
CREATE TABLE blood_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    ref_low REAL,
    ref_high REAL,
    display_order INTEGER DEFAULT 0
);

-- table blood_results
CREATE TABLE blood_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id INTEGER NOT NULL REFERENCES blood_markers(id),
    date TEXT NOT NULL,
    value REAL NOT NULL,
    value_text TEXT DEFAULT '',
    flag TEXT DEFAULT '' CHECK(flag IN ('','H','L')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(marker_id, date)
);

-- table body_measurements
CREATE TABLE body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    weight REAL,
    arm REAL,
    waist REAL,
    hips REAL,
    thighs REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- table daily_briefings
CREATE TABLE daily_briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- table daily_logs
CREATE TABLE daily_logs (
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
);

-- table exercise_aliases
CREATE TABLE exercise_aliases (
            alias TEXT PRIMARY KEY COLLATE NOCASE CHECK (length(trim(alias)) BETWEEN 1 AND 80),
            library_id INTEGER NOT NULL REFERENCES exercise_library(id) ON DELETE CASCADE
        );

-- table exercise_families
CREATE TABLE exercise_families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_order INTEGER NOT NULL DEFAULT 0
        );

-- table exercise_library
CREATE TABLE "exercise_library" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0, family_id INTEGER REFERENCES exercise_families(id) ON DELETE SET NULL, default_equipment TEXT CHECK (default_equipment IS NULL OR default_equipment IN ('barbell', 'dumbbell', 'kettlebell', 'plate', 'bodyweight', 'ball', 'band', 'machine', 'other')),
            UNIQUE(name COLLATE NOCASE)
        );

-- table exercise_library_tags
CREATE TABLE exercise_library_tags (
            library_id INTEGER NOT NULL REFERENCES exercise_library(id) ON DELETE CASCADE,
            tag TEXT NOT NULL CHECK (length(tag) BETWEEN 1 AND 40 AND tag NOT GLOB '*[^a-z0-9-]*'),
            PRIMARY KEY (library_id, tag)
        );

-- table experiment_activity_types
CREATE TABLE experiment_activity_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#3b82f6',
    display_order INTEGER DEFAULT 0,
    source_match TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'duration' CHECK(kind IN ('duration','count','boolean','scale')),
    target_value INTEGER NOT NULL DEFAULT 0,
    target_period TEXT NOT NULL DEFAULT 'week' CHECK(target_period IN ('day','week','total'))
);

-- table experiment_entries
CREATE TABLE experiment_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    activity_type_id INTEGER NOT NULL REFERENCES experiment_activity_types(id) ON DELETE CASCADE,
    value INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- table experiment_summaries
CREATE TABLE experiment_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(experiment_id, week_number)
);

-- table experiment_weeks
CREATE TABLE experiment_weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    label TEXT DEFAULT '',
    target_min INTEGER DEFAULT 0,
    target_max INTEGER DEFAULT 0,
    UNIQUE(experiment_id, week_number)
);

-- table experiments
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    start_date TEXT NOT NULL,
    num_weeks INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','abandoned')),
    created_at TEXT DEFAULT (datetime('now'))
);

-- table feniks_bricks
CREATE TABLE "feniks_bricks" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            hook TEXT NOT NULL,
            craving INTEGER CHECK(craving BETWEEN 0 AND 10),
            story TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

-- table feniks_daily
CREATE TABLE "feniks_daily" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            slipped INTEGER NOT NULL DEFAULT 0 CHECK(slipped IN (0, 1)),
            amount INTEGER,
            near_miss INTEGER NOT NULL DEFAULT 0 CHECK(near_miss IN (0, 1)),
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(habit_id, date)
        );

-- table feniks_journal
CREATE TABLE feniks_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    emotions TEXT DEFAULT '',
    triggers TEXT DEFAULT '',
    thoughts TEXT DEFAULT '',
    desired_feelings TEXT DEFAULT '',
    coping_strategies TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- table feniks_pleasures
CREATE TABLE feniks_pleasures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    pleasure_1 TEXT DEFAULT '',
    pleasure_2 TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- table goal_areas
CREATE TABLE goal_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);

-- table goal_reps
CREATE TABLE goal_reps (
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
);

-- table goals
CREATE TABLE goals (
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
);

-- table habits
CREATE TABLE habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            slip_label TEXT NOT NULL DEFAULT 'I slipped',
            slip_definition TEXT NOT NULL DEFAULT '',
            amount_unit TEXT NOT NULL DEFAULT '',
            slip_threshold INTEGER CHECK(slip_threshold IS NULL OR slip_threshold >= 1),
            near_miss_label TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL,
            target_days INTEGER NOT NULL DEFAULT 90 CHECK(target_days >= 1),
            big_why TEXT NOT NULL DEFAULT '',
            tracked INTEGER NOT NULL DEFAULT 1 CHECK(tracked IN (0, 1)),
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

-- table integrations
CREATE TABLE integrations (
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
, webhook_secret TEXT DEFAULT '');

-- table jobs
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
        CHECK(length(kind) BETWEEN 1 AND 64 AND kind = trim(kind)),
    payload_json TEXT NOT NULL DEFAULT '{}'
        CHECK(length(payload_json) <= 16384),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'needs_attention')),
    idempotency_key TEXT
        CHECK(idempotency_key IS NULL OR (
            length(idempotency_key) BETWEEN 1 AND 200
            AND idempotency_key = trim(idempotency_key)
        )),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK(attempts BETWEEN 0 AND 100),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK(max_attempts BETWEEN 1 AND 100 AND attempts <= max_attempts),
    retry_policy TEXT NOT NULL DEFAULT 'manual'
        CHECK(retry_policy IN ('automatic', 'manual')),
    run_after TEXT NOT NULL DEFAULT (datetime('now')),
    locked_at TEXT,
    locked_by TEXT CHECK(locked_by IS NULL OR length(locked_by) BETWEEN 1 AND 100),
    claim_token TEXT CHECK(claim_token IS NULL OR (
        length(claim_token) = 32 AND claim_token NOT GLOB '*[^0-9a-f]*'
    )),
    last_error TEXT NOT NULL DEFAULT '' CHECK(length(last_error) <= 500),
    result_json TEXT NOT NULL DEFAULT '{}' CHECK(length(result_json) <= 16384),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK((status = 'running') = (locked_at IS NOT NULL)),
    CHECK((status = 'running') = (locked_by IS NOT NULL)),
    CHECK((status = 'running') = (claim_token IS NOT NULL)),
    CHECK((status IN ('succeeded', 'failed', 'cancelled', 'needs_attention')) = (finished_at IS NOT NULL)),
    CHECK(status != 'queued' OR attempts < max_attempts),
    CHECK(status != 'running' OR attempts BETWEEN 1 AND max_attempts),
    CHECK(status NOT IN ('succeeded', 'failed', 'needs_attention') OR attempts BETWEEN 1 AND max_attempts),
    CHECK(status NOT IN ('running', 'succeeded', 'failed', 'needs_attention') OR started_at IS NOT NULL)
);

-- table life_scores
CREATE TABLE life_scores (
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
);

-- table llm_providers
CREATE TABLE "llm_providers" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            api_key_enc TEXT NOT NULL,
            model TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

-- table llm_publications
CREATE TABLE llm_publications (
    idempotency_key TEXT PRIMARY KEY
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)),
    kind TEXT NOT NULL
        CHECK(length(kind) BETWEEN 1 AND 64 AND kind = trim(kind)),
    job_id INTEGER NOT NULL CHECK(job_id > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- table oura_daily
CREATE TABLE oura_daily (
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
);

-- table oura_monthly
CREATE TABLE oura_monthly (
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
);

-- table oura_workouts
CREATE TABLE oura_workouts (
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
);

-- table pmo_events
CREATE TABLE "pmo_events" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'relapse' CHECK(event_type IN ('relapse','reset')),
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

-- table schema_migrations
CREATE TABLE schema_migrations (  version INTEGER PRIMARY KEY,  name TEXT NOT NULL,  applied_at TEXT NOT NULL DEFAULT (datetime('now')));

-- table sync_log
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','error')),
    message TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- table training_entries
CREATE TABLE training_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES training_exercises(id),
    set_number INTEGER NOT NULL,
    reps INTEGER,
    weight REAL,
    notes TEXT DEFAULT ''
, duration REAL, equipment TEXT CHECK (equipment IS NULL OR equipment IN ('barbell', 'dumbbell', 'kettlebell', 'plate', 'bodyweight', 'ball', 'band', 'machine', 'other')));

-- table training_exercises
CREATE TABLE training_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    section TEXT NOT NULL,
    target_sets INTEGER,
    target_reps TEXT,
    notes TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
, metric TEXT NOT NULL DEFAULT 'reps', archived INTEGER NOT NULL DEFAULT 0, ad_hoc INTEGER NOT NULL DEFAULT 0);

-- table training_sessions
CREATE TABLE training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
, wod_parsed TEXT, capture_token TEXT);

-- table user_profiles
CREATE TABLE user_profiles (
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
        );
