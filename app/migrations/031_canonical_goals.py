"""Add canonical goal state, first-class reps, and goal-linked experiments."""

import hashlib

import aiosqlite


async def _add_column(db, table: str, column: str, definition: str) -> None:
    columns = {row[1] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}
    if column not in columns:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _identity(content: str) -> str:
    return " ".join(content.split()).casefold()


async def _deduplicate_goals(db) -> None:
    rows = await db.execute_fetchall("SELECT id, area_id, horizon, content, active FROM goals ORDER BY id")
    keepers: dict[tuple[int, str, str], dict] = {}
    for row in rows:
        key = (row["area_id"], row["horizon"], _identity(row["content"]))
        keeper = keepers.get(key)
        if keeper is None:
            keepers[key] = dict(row)
            continue
        if row["active"] and not keeper["active"]:
            await db.execute("UPDATE goals SET active = 1 WHERE id = ?", (keeper["id"],))
            keeper["active"] = 1
        await db.execute("DELETE FROM goals WHERE id = ?", (row["id"],))

    for key, keeper in keepers.items():
        digest = hashlib.sha256("\x1f".join(map(str, key)).encode()).hexdigest()[:32]
        await db.execute(
            "UPDATE goals SET source = 'legacy', source_ref = ? WHERE id = ? AND source_ref = ''",
            (f"goal:{digest}", keeper["id"]),
        )


async def _rename_duplicate_metrics(db) -> None:
    rows = await db.execute_fetchall(
        "SELECT id, experiment_id, name FROM experiment_activity_types ORDER BY experiment_id, display_order, id"
    )
    seen: dict[int, set[str]] = {}
    for row in rows:
        names = seen.setdefault(row["experiment_id"], set())
        original = row["name"].strip() or "Metric"
        candidate = original[:100]
        number = 2
        while candidate.casefold() in names:
            suffix = f" ({number})"
            candidate = f"{original[: 100 - len(suffix)]}{suffix}"
            number += 1
        names.add(candidate.casefold())
        if candidate != row["name"]:
            await db.execute("UPDATE experiment_activity_types SET name = ? WHERE id = ?", (candidate, row["id"]))


async def up(db: aiosqlite.Connection) -> None:
    await _add_column(
        db,
        "goals",
        "status",
        "TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','completed','abandoned'))",
    )
    await _add_column(db, "goals", "start_date", "TEXT")
    await _add_column(db, "goals", "end_date", "TEXT")
    await _add_column(db, "goals", "completed_at", "TEXT")
    await _add_column(db, "goals", "parent_goal_id", "INTEGER REFERENCES goals(id) ON DELETE SET NULL")
    await _add_column(db, "goals", "source", "TEXT NOT NULL DEFAULT 'manual'")
    await _add_column(db, "goals", "source_ref", "TEXT NOT NULL DEFAULT ''")
    await _add_column(db, "experiments", "goal_id", "INTEGER REFERENCES goals(id) ON DELETE SET NULL")

    await _deduplicate_goals(db)
    await _rename_duplicate_metrics(db)

    await db.execute(
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
        )"""
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_goals_source_ref ON goals(source, source_ref) WHERE source_ref != ''"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_reps_source_ref ON goal_reps(source, source_ref) WHERE source_ref != ''"
    )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_reps_carried_from ON goal_reps(carried_from_id) WHERE carried_from_id IS NOT NULL"
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_goal_reps_pending ON goal_reps(status, due_date, id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_experiments_goal_id ON experiments(goal_id)")
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_experiment_metric_name ON experiment_activity_types(experiment_id, name COLLATE NOCASE)"
    )
