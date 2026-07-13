"""Onboarding's suggested experiment must be persisted with the REAL schema.

Regression: the old code inserted nonexistent experiments.weekly_target_* columns,
the exception was swallowed, and the promised experiment silently never existed.
"""

import asyncio


def _run(coro):
    return asyncio.run(coro)


async def _fresh_db(tmp_path):
    import aiosqlite

    from app.migrations.runner import run_migrations

    db = await aiosqlite.connect(tmp_path / "onboarding.db")
    db.row_factory = aiosqlite.Row
    await run_migrations(db)
    return db


def test_suggested_experiment_created_with_weeks_and_activity_type(tmp_path):
    async def scenario():
        from app.services.onboarding import create_suggested_experiment

        db = await _fresh_db(tmp_path)
        exp_id = await create_suggested_experiment(
            db,
            {
                "title": "Wieczorny spacer zamiast scrollowania",
                "description": "30 min spaceru po kolacji",
                "num_weeks": 4,
                "weekly_target_min": 90,
                "weekly_target_max": 150,
            },
        )

        exp = dict((await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (exp_id,)))[0])
        weeks = await db.execute_fetchall(
            "SELECT week_number, target_min, target_max FROM experiment_weeks WHERE experiment_id = ?", (exp_id,)
        )
        types = await db.execute_fetchall(
            "SELECT name FROM experiment_activity_types WHERE experiment_id = ?", (exp_id,)
        )
        await db.close()
        return exp, [dict(w) for w in weeks], [dict(t) for t in types]

    exp, weeks, types = _run(scenario())
    assert exp["status"] == "active"
    assert exp["num_weeks"] == 4
    assert len(weeks) == 4
    assert all(w["target_min"] == 90 and w["target_max"] == 150 for w in weeks)
    assert len(types) == 1, "An activity type is required for logging entries"


def test_suggested_experiment_garbage_input_clamped(tmp_path):
    async def scenario():
        from app.services.onboarding import create_suggested_experiment

        db = await _fresh_db(tmp_path)
        exp_id = await create_suggested_experiment(
            db,
            {
                "title": "X" * 500,
                "num_weeks": 999,  # clamped to 12
                "weekly_target_min": "not-a-number",  # default 60
                "weekly_target_max": -50,  # raised to target_min
            },
        )
        exp = dict((await db.execute_fetchall("SELECT * FROM experiments WHERE id = ?", (exp_id,)))[0])
        weeks = await db.execute_fetchall(
            "SELECT target_min, target_max FROM experiment_weeks WHERE experiment_id = ?", (exp_id,)
        )
        await db.close()
        return exp, [dict(w) for w in weeks]

    exp, weeks = _run(scenario())
    assert exp["num_weeks"] == 12
    assert len(exp["title"]) <= 200
    assert len(weeks) == 12
    assert all(w["target_min"] == 60 for w in weeks)
    assert all(w["target_max"] >= w["target_min"] for w in weeks)
