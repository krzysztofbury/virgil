from pathlib import Path

import aiosqlite

from app.config import DB_PATH

# Shared life area constants used by dashboard, life_scores, and export
LIFE_AREAS = ["planning", "spirituality", "health", "work", "social", "growth", "relaxation", "family"]
LIFE_AREA_LABELS = ["Planning", "Spirituality", "Health", "Work", "Social", "Growth", "Relaxation", "Family"]

_db: aiosqlite.Connection | None = None

# Initial schema from migration 001. See app/migrations/ for the current schema.
# Migrations add: experiment_activity_types.source_match (003),
# integrations.webhook_secret (004), app_settings.feature_no_porn (005/010),
# llm_providers rebuilt without provider CHECK (012),
# training_exercises.archived (013).
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_logs (
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

CREATE TABLE IF NOT EXISTS body_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    weight REAL,
    arm REAL,
    waist REAL,
    hips REAL,
    thighs REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feniks_config (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    start_date TEXT NOT NULL DEFAULT '2026-02-05',
    target_days INTEGER NOT NULL DEFAULT 90,
    big_why TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS feniks_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    emotions TEXT DEFAULT '',
    triggers TEXT DEFAULT '',
    thoughts TEXT DEFAULT '',
    desired_feelings TEXT DEFAULT '',
    coping_strategies TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feniks_pleasures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    pleasure_1 TEXT DEFAULT '',
    pleasure_2 TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS feniks_milestones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_number INTEGER NOT NULL UNIQUE,
    week_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    notes TEXT DEFAULT '',
    completed INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS pmo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'relapse' CHECK(event_type IN ('relapse','reset')),
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS oura_monthly (
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

CREATE TABLE IF NOT EXISTS blood_markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    unit TEXT NOT NULL,
    ref_low REAL,
    ref_high REAL,
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blood_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id INTEGER NOT NULL REFERENCES blood_markers(id),
    date TEXT NOT NULL,
    value REAL NOT NULL,
    value_text TEXT DEFAULT '',
    flag TEXT DEFAULT '' CHECK(flag IN ('','H','L')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(marker_id, date)
);

CREATE TABLE IF NOT EXISTS life_scores (
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

CREATE TABLE IF NOT EXISTS goal_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    icon TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id INTEGER NOT NULL REFERENCES goal_areas(id),
    horizon TEXT NOT NULL CHECK(horizon IN ('1yr','3yr','10yr')),
    content TEXT NOT NULL,
    display_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('success','error')),
    message TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    start_date TEXT NOT NULL,
    num_weeks INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','abandoned')),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiment_activity_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT '#3b82f6',
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS experiment_weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    label TEXT DEFAULT '',
    target_min INTEGER DEFAULT 0,
    target_max INTEGER DEFAULT 0,
    UNIQUE(experiment_id, week_number)
);

CREATE TABLE IF NOT EXISTS experiment_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    activity_type_id INTEGER NOT NULL REFERENCES experiment_activity_types(id) ON DELETE CASCADE,
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiment_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    week_number INTEGER NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(experiment_id, week_number)
);

CREATE TABLE IF NOT EXISTS oura_workouts (
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

CREATE TABLE IF NOT EXISTS integrations (
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
);

CREATE TABLE IF NOT EXISTS oura_daily (
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

CREATE TABLE IF NOT EXISTS llm_providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    api_key_enc TEXT NOT NULL,
    model TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS training_exercises (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    section TEXT NOT NULL,
    target_sets INTEGER,
    target_reps TEXT,
    notes TEXT DEFAULT '',
    display_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS training_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    duration_minutes INTEGER,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS training_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
    exercise_id INTEGER NOT NULL REFERENCES training_exercises(id),
    set_number INTEGER NOT NULL,
    reps INTEGER,
    weight REAL,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret TEXT DEFAULT '',
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS daily_briefings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
"""

SEED_FENIKS_CONFIG = """
INSERT OR IGNORE INTO feniks_config (id, start_date, target_days, big_why)
VALUES (1, '2026-02-05', 90, 'Porno niszczy Twoją sprawczość i męską energię. Odzyskanie suwerenności nad układem dopaminowym i integracja Cienia.');
"""

SEED_GOAL_AREAS = """
INSERT OR IGNORE INTO goal_areas (name, icon, display_order) VALUES
('Planowanie Życia', '📋', 1),
('Duchowość', '✨', 2),
('Zdrowie', '💪', 3),
('Praca', '💼', 4),
('Życie Towarzyskie', '👥', 5),
('Rozwój', '🎓', 6),
('Relaks', '🎮', 7),
('Rodzina', '👨‍👩‍👧‍👦', 8);
"""

SEED_MILESTONES = """
INSERT OR IGNORE INTO feniks_milestones (day_number, week_number, title) VALUES
(1, 1, 'Jak korzystać z tego programu?'),
(2, 1, 'Ognisko i jak przestać walczyć z dymem?'),
(3, 1, 'Poznaj swoje paliwo: Tabela z Ogniskiem'),
(4, 1, 'Trening mózgu: rozpoznawanie ognia, paliwa i zapalników'),
(5, 1, 'Co daje mi porno? (Szczera analiza)'),
(6, 1, 'Analiza korzyści (Bilans zysków i strat)'),
(7, 1, 'Emocje czy sfrustrowane potrzeby?'),
(8, 2, 'Kręgle, droga środka i oswojenie bestii'),
(9, 2, 'Płotki na szlaku – metody ograniczenia dostępu'),
(10, 2, 'Zmiana motywacji'),
(11, 2, 'Mogę, ale nie chcę'),
(12, 2, '2 przyjemności'),
(13, 2, 'Przyjemności nie są zamiennikiem'),
(14, 2, 'Przygotowanie do wyznaczenia celu'),
(15, 3, 'Drabinka celów'),
(16, 3, 'Wskazówki dla różnych celów (A/B/C)'),
(17, 3, 'Wskazówki dla różnych celów (A/B/C)'),
(18, 3, 'Wskazówki dla różnych celów (A/B/C)'),
(19, 3, 'Mój Cel + Ta jedna rzecz na dzisiaj'),
(20, 3, 'Najdłuższe okresy bez porno'),
(21, 3, 'Podsumowanie'),
(22, 4, 'Czego się nauczyłem?'),
(23, 4, 'Identyfikacja ognisk zapalnych'),
(24, 4, 'Mapa ważnych obszarów życia'),
(25, 4, 'Priorytety na 10 dni'),
(26, 4, 'Reaktor atomowy'),
(27, 4, 'Zniekształcenia poznawcze (Debugowanie mózgu)'),
(28, 4, 'Zniekształcenia poznawcze (Debugowanie mózgu)'),
(29, 5, 'Musy i powinności'),
(30, 5, 'Musy i powinności'),
(31, 5, 'Myślenie czarnobiałe i perfekcjonizm'),
(32, 5, 'Myślenie czarnobiałe i perfekcjonizm'),
(33, 5, 'Podsumowanie 30 dni'),
(34, 5, '2 przyjemności - check'),
(35, 5, 'Super ładowarki'),
(36, 6, 'Uogólnienia i etykiety'),
(37, 6, 'Uogólnienia i etykiety'),
(38, 6, 'Czytanie w myślach i lęki społeczne'),
(39, 6, 'Czytanie w myślach i lęki społeczne'),
(40, 6, 'Przeskakiwanie do konkluzji'),
(41, 6, 'Przeskakiwanie do konkluzji'),
(42, 6, 'Sprawczość nad seksualnością'),
(43, 7, 'Pomijanie pozytywów'),
(44, 7, 'Pomijanie pozytywów'),
(45, 7, 'Odpoczynek (Połowa!)'),
(46, 7, 'Góra lodowa (Przekonania kluczowe)'),
(47, 7, 'Sprawczość nad nastrojem'),
(48, 7, 'Mój pryzmat i filtr mentalny'),
(49, 7, 'Mój pryzmat i filtr mentalny'),
(50, 8, 'Praca z cegłami przekonań (przeszłość)'),
(51, 8, 'Praca z cegłami przekonań (przeszłość)'),
(52, 8, 'Praca z cegłami przekonań (trudne)'),
(53, 8, 'Praca z cegłami przekonań (trudne)'),
(54, 8, 'Praca z cegłami przekonań (pozytywne)'),
(55, 8, 'Praca z cegłami przekonań (pozytywne)'),
(56, 8, 'Praca z cegłami przekonań (pozytywne)'),
(57, 9, 'Kompensacja'),
(58, 9, 'Unikanie'),
(59, 9, 'Konfrontacja'),
(60, 9, 'Pogłębiona ekspozycja (Nurkowanie)'),
(61, 9, 'Pogłębiona ekspozycja (Nurkowanie)'),
(62, 9, 'Pogłębiona ekspozycja (Nurkowanie)'),
(63, 9, 'Stan przyjemności'),
(64, 10, 'List pożegnalny i ambiwalencja'),
(65, 10, 'List pożegnalny i ambiwalencja'),
(66, 10, 'List pożegnalny i ambiwalencja'),
(67, 10, 'List pożegnalny i ambiwalencja'),
(68, 10, 'Odpoczynek'),
(69, 10, 'Przekonania podtrzymujące nałóg'),
(70, 10, 'Przekonania podtrzymujące nałóg'),
(71, 11, 'Identyfikacja ognisk'),
(72, 11, 'Identyfikacja ognisk'),
(73, 11, 'Lęki vs okoliczności'),
(74, 11, 'Lęki vs okoliczności'),
(75, 11, 'Cele długoterminowe'),
(76, 11, 'Cele długoterminowe'),
(77, 11, 'Cele długoterminowe'),
(78, 12, 'Lista żali'),
(79, 12, 'Lista żali'),
(80, 12, 'Przebaczenie sobie'),
(81, 12, 'Przebaczenie sobie'),
(82, 12, 'Domknięcie przeszłości'),
(83, 12, 'Domknięcie przeszłości'),
(84, 12, 'Odpoczynek'),
(85, 13, 'Plan na nawroty (Safety Net)'),
(86, 13, 'Plan na nawroty (Safety Net)'),
(87, 13, 'Cele życiowe i memento mori'),
(88, 13, 'Cele życiowe i memento mori'),
(89, 13, 'Podsumowanie i Checkpoint'),
(90, 13, 'Podsumowanie i Checkpoint');
"""


SEED_APP_SETTINGS = """
INSERT OR IGNORE INTO app_settings (key, value) VALUES
('backup_enabled', '1'),
('backup_interval_hours', '24'),
('backup_max_copies', '7'),
('oura_sync_enabled', '0'),
('oura_sync_interval_hours', '6'),
('briefing_enabled', '0');
"""

SEED_TRAINING_EXERCISES = """
INSERT OR IGNORE INTO training_exercises (id, name, section, target_sets, target_reps, notes, display_order) VALUES
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
(19, 'Shoulder Mobility', 'Stretching', 1, '5 min', '', 19);
"""


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        # Health check: verify the connection is still alive
        try:
            await _db.execute("SELECT 1")
        except Exception:
            _db = None
    if _db is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row

        # Verify PRAGMAs took effect — do not rely on silent success.
        result = await _db.execute_fetchall("PRAGMA journal_mode=WAL")
        assert result[0][0].lower() == "wal", f"WAL mode not enabled: {result}"

        await _db.execute("PRAGMA foreign_keys=ON")
        fk_result = await _db.execute_fetchall("PRAGMA foreign_keys")
        assert fk_result[0][0] == 1, "Foreign keys not enabled"

    return _db


async def init_db():
    db = await get_db()
    from app.migrations.runner import run_migrations

    await run_migrations(db)


async def get_setting(db, key: str, default: str = "") -> str:
    assert key, "Setting key must not be empty"
    row = await db.execute_fetchall("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row[0]["value"] if row else default


async def set_setting(db, key: str, value: str) -> None:
    assert key, "Setting key must not be empty"
    assert isinstance(value, str), f"Setting value must be str, got {type(value).__name__}"
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )
    await db.commit()


async def get_feature_flags(db) -> dict[str, bool]:
    """Load all feature_* settings as a {name: bool} dict."""
    rows = await db.execute_fetchall("SELECT key, value FROM app_settings WHERE key LIKE 'feature_%'")
    return {row["key"].removeprefix("feature_"): row["value"] == "1" for row in rows}


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
