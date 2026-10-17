"""English names for the labels the app itself wrote in Polish.

The eight seeded goal areas, and the blood-test categories the markdown importer
assigned, get their English names. Only exact matches change: an area or category
the person named themselves keeps its name. Marker names and goals stay as written.
"""

import aiosqlite

GOAL_AREAS = {
    "Planowanie Życia": "Life Planning",
    "Duchowość": "Spirituality",
    "Zdrowie": "Health",
    "Praca": "Work",
    "Życie Towarzyskie": "Social Life",
    "Rozwój": "Growth",
    "Relaks": "Relaxation",
    "Rodzina": "Family",
}

BLOOD_CATEGORIES = {
    "Morfologia": "Blood count",
    "Tarczyca i Hormony": "Thyroid & hormones",
    "Metabolizm": "Metabolic",
    "Lipidy": "Lipids",
    "Witaminy i Minerały": "Vitamins & minerals",
    "Stany zapalne": "Inflammation",
    "Wątroba i Nerki": "Liver & kidneys",
    "Inne": "Other",
}


async def up(db: aiosqlite.Connection) -> None:
    for polish, english in GOAL_AREAS.items():
        # goal_areas.name is UNIQUE: never collide with an area already named in English.
        await db.execute(
            "UPDATE goal_areas SET name = ? WHERE name = ? AND NOT EXISTS (SELECT 1 FROM goal_areas WHERE name = ?)",
            (english, polish, english),
        )
    for polish, english in BLOOD_CATEGORIES.items():
        await db.execute("UPDATE blood_markers SET category = ? WHERE category = ?", (english, polish))
