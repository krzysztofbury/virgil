"""Migration 036: the app's own Polish labels become English; the person's own names stay."""

import asyncio
import importlib
import re
from pathlib import Path

import aiosqlite

MIGRATIONS = Path(__file__).resolve().parents[1] / "app" / "migrations"


async def _db_at_035(path):
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    for file in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.py")):
        if int(re.match(r"\d{3}", file.name).group()) > 35:
            continue
        await importlib.import_module(f"app.migrations.{file.stem}").up(db)
        await db.commit()
    return db


def test_seeded_labels_turn_english_and_custom_names_stay(tmp_path):
    migration = importlib.import_module("app.migrations.036_english_labels")

    async def scenario():
        db = await _db_at_035(tmp_path / "upgrade.db")
        try:
            # An account created before the seed was English, with one area renamed by hand.
            polish = ["Planowanie Życia", "Duchowość", "Zdrowie", "Praca", "Moja firma", "Rozwój", "Relaks", "Rodzina"]
            for order, name in enumerate(polish, 1):
                await db.execute("UPDATE goal_areas SET name = ? WHERE display_order = ?", (name, order))
            await db.execute(
                "INSERT INTO blood_markers (name, category, unit) VALUES "
                "('Ferrytyna', 'Witaminy i Minerały', 'ng/mL'), ('Moje badanie', 'Własne', '')"
            )
            await db.commit()
            for _ in range(2):  # a crashed run is simply repeated
                await migration.up(db)
                await db.commit()
            areas = await db.execute_fetchall("SELECT name FROM goal_areas ORDER BY display_order")
            markers = await db.execute_fetchall("SELECT name, category FROM blood_markers ORDER BY name")
            return [r["name"] for r in areas], [tuple(r) for r in markers]
        finally:
            await db.close()

    areas, markers = asyncio.run(scenario())
    assert areas == [
        "Life Planning",
        "Spirituality",
        "Health",
        "Work",
        "Moja firma",
        "Growth",
        "Relaxation",
        "Family",
    ]
    assert markers == [("Ferrytyna", "Vitamins & minerals"), ("Moje badanie", "Własne")]
