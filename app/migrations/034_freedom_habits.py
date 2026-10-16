"""Freedom: the No Porn module becomes one habit among any number of them.

- `habits` holds each habit being broken, with its own slip definition, unit,
  near-miss label, start date, target and "why" (feniks_config held these for
  the one module there was).
- feniks_daily, feniks_bricks and pmo_events gain `habit_id`; the day log's
  porn-specific columns become generic: used -> slipped, minutes -> amount,
  edging -> near_miss. One day per habit replaces one day overall.
- The feature flag feature_no_porn becomes feature_freedom.

An account that used the module (flag on, or any day, brick or relapse) gets a
"Porn" habit carrying its config, and every existing row moves onto it. Fresh
installs get the same tables from here, with no habit until one is chosen.

Crash-retry safety, as in 023: each table is rebuilt only while it still lacks
`habit_id`, the scratch table is dropped first, and the Porn habit is inserted
with INSERT OR IGNORE so a retry finds it again.
"""

import aiosqlite

# Frozen here, not imported from app.services.habits: a migration must keep
# doing what it did when it shipped, whatever the live catalog says later.
PORN = {
    "name": "Porn",
    "slip_label": "I watched",
    "amount_unit": "minutes",
    "slip_threshold": 5,
    "near_miss_label": "Edging",
    "slip_definition": (
        "Watched means at least 5 total minutes of intentional viewing in one day. "
        "A shorter exposure stays a clean outcome; record it in the day note."
    ),
}


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    return {c[1] for c in await db.execute_fetchall(f"PRAGMA table_info({table})")}


async def _rebuild(db: aiosqlite.Connection, table: str, create: str, copy: str, porn_id: int | None) -> None:
    if "habit_id" in await _columns(db, table):
        return
    await db.execute(f"DROP TABLE IF EXISTS {table}_new")
    await db.execute(create)
    if porn_id is not None:
        await db.execute(copy, (porn_id,))
    await db.execute(f"DROP TABLE {table}")
    await db.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS habits (
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
        )
        """
    )

    flag = await db.execute_fetchall("SELECT value FROM app_settings WHERE key = 'feature_no_porn'")
    if flag:
        await db.execute(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('feature_freedom', ?)", (flag[0]["value"],)
        )
        await db.execute("DELETE FROM app_settings WHERE key = 'feature_no_porn'")
    enabled = await db.execute_fetchall("SELECT 1 FROM app_settings WHERE key = 'feature_freedom' AND value = '1'")

    used_before = False
    for table in ("feniks_daily", "feniks_bricks", "pmo_events"):
        if "habit_id" not in await _columns(db, table):
            used_before = used_before or bool(await db.execute_fetchall(f"SELECT 1 FROM {table} LIMIT 1"))

    porn_id = None
    existing = await db.execute_fetchall("SELECT id FROM habits WHERE slug = 'porn'")
    if existing:
        porn_id = existing[0]["id"]
    elif enabled or used_before:
        preset = PORN
        config = await db.execute_fetchall("SELECT start_date, target_days, big_why FROM feniks_config WHERE id = 1")
        config = dict(config[0]) if config else {}
        cursor = await db.execute(
            "INSERT OR IGNORE INTO habits (slug, name, slip_label, slip_definition, amount_unit, slip_threshold, "
            "near_miss_label, start_date, target_days, big_why, position) "
            "VALUES ('porn', ?, ?, ?, ?, ?, ?, COALESCE(?, date('now')), COALESCE(?, 90), COALESCE(?, ''), 0)",
            (
                preset["name"],
                preset["slip_label"],
                preset["slip_definition"],
                preset["amount_unit"],
                preset["slip_threshold"],
                preset["near_miss_label"],
                config.get("start_date"),
                config.get("target_days"),
                config.get("big_why"),
            ),
        )
        porn_id = cursor.lastrowid

    await _rebuild(
        db,
        "feniks_daily",
        """
        CREATE TABLE feniks_daily_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            slipped INTEGER NOT NULL DEFAULT 0 CHECK(slipped IN (0, 1)),
            amount INTEGER,
            near_miss INTEGER NOT NULL DEFAULT 0 CHECK(near_miss IN (0, 1)),
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(habit_id, date)
        )
        """,
        "INSERT INTO feniks_daily_new (id, habit_id, date, slipped, amount, near_miss, note, created_at) "
        "SELECT id, ?, date, used, minutes, edging, note, created_at FROM feniks_daily",
        porn_id,
    )
    await _rebuild(
        db,
        "feniks_bricks",
        """
        CREATE TABLE feniks_bricks_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            hook TEXT NOT NULL,
            craving INTEGER CHECK(craving BETWEEN 0 AND 10),
            story TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """,
        "INSERT INTO feniks_bricks_new (id, habit_id, date, hook, craving, story, created_at) "
        "SELECT id, ?, date, hook, craving, story, created_at FROM feniks_bricks",
        porn_id,
    )
    await _rebuild(
        db,
        "pmo_events",
        """
        CREATE TABLE pmo_events_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
            date TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'relapse' CHECK(event_type IN ('relapse','reset')),
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        )
        """,
        "INSERT INTO pmo_events_new (id, habit_id, date, event_type, notes, created_at) "
        "SELECT id, ?, date, event_type, notes, created_at FROM pmo_events",
        porn_id,
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_pmo_events_habit_date ON pmo_events(habit_id, date)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_feniks_bricks_habit_date ON feniks_bricks(habit_id, date)")
