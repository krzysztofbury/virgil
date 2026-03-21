"""Initial schema and seed data."""

import aiosqlite

from app.db import (
    SCHEMA,
    SEED_APP_SETTINGS,
    SEED_FENIKS_CONFIG,
    SEED_GOAL_AREAS,
    SEED_MILESTONES,
    SEED_TRAINING_EXERCISES,
)


async def up(db: aiosqlite.Connection) -> None:
    for statement in SCHEMA.split(";"):
        stmt = statement.strip()
        if stmt:
            await db.execute(stmt + ";")
    await db.executescript(SEED_FENIKS_CONFIG)
    await db.executescript(SEED_GOAL_AREAS)
    await db.executescript(SEED_MILESTONES)
    await db.executescript(SEED_TRAINING_EXERCISES)
    await db.executescript(SEED_APP_SETTINGS)
