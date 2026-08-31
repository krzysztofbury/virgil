import asyncio
import importlib

import aiosqlite

migration = importlib.import_module("app.migrations.030_feniks_measurement_contract")


def test_migration_preserves_hidden_clean_measurements_in_the_note(tmp_path):
    async def scenario():
        db = await aiosqlite.connect(tmp_path / "feniks.db")
        db.row_factory = aiosqlite.Row
        await db.executescript(
            """
            CREATE TABLE feniks_daily (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL UNIQUE,
                used INTEGER NOT NULL,
                minutes INTEGER,
                edging INTEGER NOT NULL,
                note TEXT
            );
            CREATE TABLE pmo_events (
                id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                event_type TEXT NOT NULL,
                notes TEXT
            );
            INSERT INTO feniks_daily VALUES (1, '2026-08-01', 0, 3, 1, 'short exposure');
            INSERT INTO feniks_daily VALUES (2, '2026-08-02', 1, 20, 1, 'watched');
            INSERT INTO feniks_daily VALUES (3, '2026-08-03', 1, NULL, 0, 'n' || printf('%0*d', 500, 0));
            INSERT INTO feniks_daily VALUES (4, '2026-08-04', 1, 4, 1, 'short watched');
            INSERT INTO pmo_events VALUES (1, '2026-08-03', 'relapse', 'via daily log');
            INSERT INTO pmo_events VALUES (2, '2026-08-04', 'relapse', 'manual history');
            """
        )
        await migration.up(db)
        rows = await db.execute_fetchall("SELECT used, minutes, edging, note FROM feniks_daily ORDER BY id")
        events = await db.execute_fetchall("SELECT date, notes FROM pmo_events ORDER BY id")
        await db.close()
        return [tuple(row) for row in rows], [tuple(row) for row in events]

    rows, events = asyncio.run(scenario())
    assert rows[0] == (0, None, 0, "[legacy clean measurement: 3 min; edging yes] short exposure")
    assert rows[1] == (1, 20, 1, "watched")
    assert rows[2][0:3] == (0, None, 0)
    assert rows[2][3].startswith("[legacy watched outcome below 5-minute contract: minutes not recorded] n")
    assert rows[2][3].endswith("0" * 500)
    assert rows[3] == (
        0,
        None,
        0,
        "[legacy watched outcome below 5-minute contract: 4 min; edging yes] short watched",
    )
    assert events == [("2026-08-04", "manual history")]
