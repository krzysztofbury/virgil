import asyncio
import importlib

import aiosqlite

migration = importlib.import_module("app.migrations.031_canonical_goals")


def test_migration_deduplicates_goals_and_disambiguates_metrics(tmp_path):
    async def scenario():
        db = await aiosqlite.connect(tmp_path / "migration.db")
        db.row_factory = aiosqlite.Row
        await db.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE goal_areas (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
            INSERT INTO goal_areas VALUES (1, 'Health');
            CREATE TABLE goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                area_id INTEGER NOT NULL REFERENCES goal_areas(id),
                horizon TEXT NOT NULL,
                content TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                display_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            INSERT INTO goals(area_id, horizon, content, active) VALUES (1, '1yr', 'Same Goal', 0);
            INSERT INTO goals(area_id, horizon, content, active) VALUES (1, '1yr', '  same   goal ', 1);
            CREATE TABLE experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                start_date TEXT NOT NULL,
                num_weeks INTEGER NOT NULL
            );
            INSERT INTO experiments(title, start_date, num_weeks) VALUES ('Probe', '2026-08-01', 4);
            CREATE TABLE experiment_activity_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id INTEGER NOT NULL REFERENCES experiments(id),
                name TEXT NOT NULL,
                display_order INTEGER DEFAULT 0
            );
            INSERT INTO experiment_activity_types(experiment_id, name, display_order) VALUES (1, 'Gate', 1);
            INSERT INTO experiment_activity_types(experiment_id, name, display_order) VALUES (1, 'gate', 2);
            """
        )
        await migration.up(db)
        goals = await db.execute_fetchall("SELECT active, source, source_ref FROM goals")
        metrics = await db.execute_fetchall("SELECT name FROM experiment_activity_types ORDER BY id")
        rep_columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(goal_reps)")}
        await db.close()
        return goals, metrics, rep_columns

    goals, metrics, rep_columns = asyncio.run(scenario())
    assert len(goals) == 1
    assert goals[0]["active"] == 1
    assert goals[0]["source"] == "legacy"
    assert goals[0]["source_ref"]
    assert [row["name"] for row in metrics] == ["Gate", "gate (2)"]
    assert {"period", "due_date", "status", "completed_at", "carried_from_id"} <= rep_columns
