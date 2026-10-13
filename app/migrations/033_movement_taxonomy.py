"""Movement families, aliases and equipment for the exercise library.

The vocabulary was a flat list that missed movements the athlete actually
trains (pistols, wall walks, D-ball over shoulder, devil press, hang power
snatch...). A closed vocabulary with gaps does not stay empty: the nearest
listed name gets picked instead, so "50 pistols" became Sit-up and
"ball over shoulder" became Wall Ball.

This migration adds:
- exercise_families, and exercise_library.family_id pointing at one of them,
  so variants roll up ("every snatch variant") without combining attributes;
- exercise_library.default_equipment, and training_entries.equipment for the
  set itself, so a dumbbell hang snatch and a barbell one stay one movement
  with separate loads;
- exercise_aliases, the athlete's spellings ("T2B", "pistols") mapped onto a
  library name, so matching is exact name, then alias, then unknown;
- the missing movements, and two duplicate pairs merged with their history.

The data below is frozen on purpose. A migration that imported a list from
application code would change meaning whenever that list is edited.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)

EQUIPMENT = ("barbell", "dumbbell", "kettlebell", "plate", "bodyweight", "ball", "band", "machine", "other")

FAMILIES = (
    "Snatch",
    "Clean",
    "Press & Jerk",
    "Squat",
    "Hinge",
    "Lunge",
    "Pull",
    "Push",
    "Handstand",
    "Core",
    "Plyo & Odd Object",
    "Carry",
    "Monostructural",
    "Accessory",
    "Mobility",
)

# name, section, metric, family, default equipment
NEW_MOVEMENTS = (
    ("Hang Snatch", "Core", "reps", "Snatch", "barbell"),
    ("Hang Power Snatch", "Core", "reps", "Snatch", "barbell"),
    ("Snatch High-Pull", "Core", "reps", "Snatch", "barbell"),
    ("Snatch Balance", "Core", "reps", "Snatch", "barbell"),
    ("Hang Clean", "Core", "reps", "Clean", "barbell"),
    ("Hang Power Clean", "Core", "reps", "Clean", "barbell"),
    ("Clean & Jerk", "Core", "reps", "Clean", "barbell"),
    ("Split Jerk", "Core", "reps", "Press & Jerk", "barbell"),
    ("Shoulder to Overhead", "Core", "reps", "Press & Jerk", "barbell"),
    ("Pistol Squat", "Core", "reps", "Squat", "bodyweight"),
    ("Sumo Deadlift High Pull", "Core", "reps", "Hinge", "barbell"),
    ("Walking Lunge", "Core", "reps", "Lunge", "bodyweight"),
    ("Overhead Lunge", "Core", "reps", "Lunge", None),
    ("Ring Row", "Core", "reps", "Pull", "bodyweight"),
    ("Burpee Pull-up", "Core", "reps", "Pull", "bodyweight"),
    ("Dead Hang", "Core", "time", "Pull", "bodyweight"),
    ("Handstand Walk", "Core", "time", "Handstand", "bodyweight"),
    ("Wall Walk", "Core", "reps", "Handstand", "bodyweight"),
    ("Handstand Hold", "Core", "time", "Handstand", "bodyweight"),
    ("Knees-to-elbow", "Core", "reps", "Core", "bodyweight"),
    ("GHD Sit-up", "Core", "reps", "Core", "machine"),
    ("L-sit", "Core", "time", "Core", "bodyweight"),
    ("Hollow Hold", "Core", "time", "Core", "bodyweight"),
    ("Box Jump Over", "Core", "reps", "Plyo & Odd Object", "bodyweight"),
    ("Box Step-over", "Core", "reps", "Plyo & Odd Object", None),
    ("D-ball Over Shoulder", "Core", "reps", "Plyo & Odd Object", "ball"),
    ("Devil Press", "Core", "reps", "Plyo & Odd Object", "dumbbell"),
    ("Arm Circles", "Warmup", "time", "Mobility", "bodyweight"),
)

# Existing names: family, default equipment. Missing rows are skipped.
EXISTING = {
    "Snatch": ("Snatch", "barbell"),
    "Power Snatch": ("Snatch", "barbell"),
    "High Hang Snatch": ("Snatch", "barbell"),
    "Clean": ("Clean", "barbell"),
    "Power Clean": ("Clean", "barbell"),
    "KB Clean": ("Clean", "kettlebell"),
    "Overhead Press": ("Press & Jerk", "barbell"),
    "Push Press": ("Press & Jerk", "barbell"),
    "Push Jerk": ("Press & Jerk", "barbell"),
    "Seated KB Press": ("Press & Jerk", "kettlebell"),
    "Air Squat": ("Squat", "bodyweight"),
    "Back Squat": ("Squat", "barbell"),
    "Front Squat": ("Squat", "barbell"),
    "Overhead Squat": ("Squat", "barbell"),
    "Goblet Squat": ("Squat", "kettlebell"),
    "Thruster": ("Squat", "barbell"),
    "Deadlift": ("Hinge", "barbell"),
    "Romanian Deadlift": ("Hinge", "kettlebell"),
    "KB Swing": ("Hinge", "kettlebell"),
    "KB Swing Intervals": ("Hinge", "kettlebell"),
    "Lunge": ("Lunge", "bodyweight"),
    "KB Lunge": ("Lunge", "kettlebell"),
    "Pull-up": ("Pull", "bodyweight"),
    "Chest-to-bar Pull-up": ("Pull", "bodyweight"),
    "Muscle-up": ("Pull", "bodyweight"),
    "Bent-over Row": ("Pull", None),
    "Barbell Row": ("Pull", "barbell"),
    "Single-arm Row": ("Pull", "kettlebell"),
    "Renegade Row": ("Pull", "kettlebell"),
    "Lat Pulldown": ("Pull", "machine"),
    "Push-up": ("Push", "bodyweight"),
    "Push-up on KB": ("Push", "kettlebell"),
    "Diamond Push-up": ("Push", "bodyweight"),
    "Dip (chairs/bars)": ("Push", "bodyweight"),
    "Ring Dip": ("Push", "bodyweight"),
    "Bench Press": ("Push", "barbell"),
    "Floor Press": ("Push", "kettlebell"),
    "Handstand Push-up": ("Handstand", "bodyweight"),
    "Toes-to-bar": ("Core", "bodyweight"),
    "Hanging Knee Raise": ("Core", "bodyweight"),
    "Sit-up": ("Core", "bodyweight"),
    "Plank": ("Core", "bodyweight"),
    "Side Plank": ("Core", "bodyweight"),
    "Dead Bug": ("Core", "bodyweight"),
    "Pallof Press (band)": ("Core", "band"),
    "Box Jump": ("Plyo & Odd Object", "bodyweight"),
    "Burpee": ("Plyo & Odd Object", "bodyweight"),
    "Wall Ball": ("Plyo & Odd Object", "ball"),
    "Farmer's Walk": ("Carry", "kettlebell"),
    "Row": ("Monostructural", "machine"),
    "Bike": ("Monostructural", "other"),
    "Assault Bike": ("Monostructural", "machine"),
    "Ski Erg": ("Monostructural", "machine"),
    "Run": ("Monostructural", "bodyweight"),
    "Single-under": ("Monostructural", "other"),
    "Double-under": ("Monostructural", "other"),
    "Jump Rope": ("Monostructural", "other"),
    "Swimming": ("Monostructural", "bodyweight"),
    "Zone 2 Walk (dog)": ("Monostructural", "bodyweight"),
    "Boxing Bag Rounds": ("Monostructural", "other"),
    "Jumping Jacks": ("Mobility", "bodyweight"),
    "Biceps Curl": ("Accessory", "dumbbell"),
    "Triceps Extension": ("Accessory", "dumbbell"),
    "KB Halo": ("Accessory", "kettlebell"),
    "Turkish Get-up": ("Accessory", "kettlebell"),
    "Band Pull-apart": ("Mobility", "band"),
    "Thoracic Extension (roller)": ("Mobility", "other"),
    "Goblet Hold (deep squat)": ("Mobility", "kettlebell"),
    "Band Dislocates": ("Mobility", "band"),
    "Hamstring Stretch": ("Mobility", "bodyweight"),
    "Hip Flexor Stretch": ("Mobility", "bodyweight"),
    "Couch Stretch": ("Mobility", "bodyweight"),
    "Child's Pose": ("Mobility", "bodyweight"),
    "Box Breathing": ("Mobility", None),
    "Full Body Stretch": ("Mobility", "bodyweight"),
    "Upper Body Stretch": ("Mobility", "bodyweight"),
    "Lower Body Stretch": ("Mobility", "bodyweight"),
}

ALIASES = {
    "Snatch": ("squat snatch", "full snatch"),
    "Power Snatch": ("power snatches",),
    "High Hang Snatch": ("high hang power snatch", "high-hang snatch"),
    "Hang Power Snatch": ("HPS", "hang power snatches"),
    "Hang Snatch": ("hang squat snatch",),
    "Snatch High-Pull": ("snatch high pull", "snatch pull"),
    "Clean": ("squat clean", "full clean"),
    "Power Clean": ("power cleans",),
    "Hang Power Clean": ("HPC", "hang power cleans"),
    "Hang Clean": ("hang squat clean",),
    "Clean & Jerk": ("clean and jerk", "C&J"),
    "Overhead Press": ("strict press", "shoulder press", "press", "OHP"),
    "Push Jerk": ("jerk",),
    "Shoulder to Overhead": ("S2OH", "STOH"),
    "Air Squat": ("squat", "squats", "air squats", "bodyweight squat"),
    "Back Squat": ("back squats",),
    "Front Squat": ("front squats",),
    "Overhead Squat": ("OHS",),
    "Goblet Squat": ("goblet squats",),
    "Thruster": ("thrusters",),
    "Pistol Squat": ("pistol", "pistols", "pistol squats"),
    "Deadlift": ("dead lift", "deadlifts"),
    "Romanian Deadlift": ("RDL",),
    "KB Swing": ("kettlebell swing", "swing", "swings", "russian swing"),
    "Sumo Deadlift High Pull": ("SDHP",),
    "Lunge": ("lunges",),
    "Walking Lunge": ("walking lunges",),
    "Overhead Lunge": ("OH lunge", "overhead walking lunge"),
    "Pull-up": ("pull ups", "pull-ups", "pullup", "pullups", "kipping pull-up", "strict pull-up"),
    "Chest-to-bar Pull-up": ("C2B", "chest to bar"),
    "Muscle-up": ("bar muscle-up", "ring muscle-up"),
    "Ring Row": ("australian pull-up", "australian pull ups", "inverted row"),
    "Burpee Pull-up": ("burpee pull ups", "burpee pull-ups"),
    "Dead Hang": ("dead hangs",),
    "Push-up": ("push ups", "pushups", "pushup", "push-ups"),
    "Ring Dip": ("ring dips",),
    "Dip (chairs/bars)": ("dip", "dips"),
    "Handstand Push-up": ("HSPU", "handstand push ups", "head stand push up", "headstand push-up"),
    "Handstand Walk": ("hand stand walk", "HS walk"),
    "Wall Walk": ("wall climb", "wall climbs", "wall walks"),
    "Handstand Hold": ("hand stand hold", "wall handstand"),
    "Toes-to-bar": ("T2B", "toes to bar"),
    "Knees-to-elbow": ("K2E", "knees to elbow"),
    "Sit-up": ("sit ups", "situps", "sit-ups", "abmat sit-up"),
    "L-sit": ("l sit",),
    "Hollow Hold": ("hollow body hold",),
    "Box Jump": ("box jumps",),
    "Box Jump Over": ("box jump overs", "BJO"),
    "Box Step-over": ("box step overs", "step overs", "step-ups"),
    "Burpee": ("burpees",),
    "Wall Ball": ("wall balls", "wall ball shots"),
    "D-ball Over Shoulder": ("ball over shoulder", "ball over shoulders", "sandbag over shoulder"),
    "Devil Press": ("devil presses",),
    "Farmer's Walk": ("farmers walk", "farmer carry"),
    "Row": ("rowing", "rower", "erg", "row erg"),
    "Bike": ("cycling", "bike ride", "riding a bike"),
    "Assault Bike": ("air bike", "echo bike"),
    "Ski Erg": ("ski", "skierg"),
    "Run": ("running",),
    "Single-under": ("single unders", "rope jumps", "rope jump"),
    "Double-under": ("DU", "double unders"),
    "Swimming": ("swim",),
    "KB Halo": ("halo",),
    "Full Body Stretch": ("stretching", "full body stretching"),
}

# Duplicate pairs: the first name's history moves to the second, the first
# becomes an alias. Training rows are matched by name, as resolve_movement does.
MERGES = (
    ("Kettlebell Swing", "KB Swing"),
    ("Shoulder Press", "Overhead Press"),
    ("Halo (KB)", "KB Halo"),
)

# A builtin seeded with the wrong metric: its target is "12-15" reps.
METRIC_FIXES = (("Hanging Knee Raise", "reps"),)


async def _columns(db, table: str) -> set[str]:
    return {row[1] for row in await db.execute_fetchall(f"PRAGMA table_info({table})")}


async def _library_id(db, name: str) -> int | None:
    rows = await db.execute_fetchall("SELECT id FROM exercise_library WHERE lower(name) = lower(?)", (name,))
    return rows[0][0] if rows else None


async def _tex_id(db, name: str) -> int | None:
    rows = await db.execute_fetchall("SELECT id FROM training_exercises WHERE lower(name) = lower(?)", (name,))
    return rows[0][0] if rows else None


async def _merge(db, source: str, target: str) -> None:
    target_lib = await _library_id(db, target)
    if target_lib is None:
        logger.warning("merge %r -> %r skipped: target is not in the exercise library", source, target)
        return
    source_tex = await _tex_id(db, source)
    if source_tex is not None:
        target_tex = await _tex_id(db, target)
        if target_tex is None:
            target_name = (await db.execute_fetchall("SELECT name FROM exercise_library WHERE id = ?", (target_lib,)))[
                0
            ][0]
            await db.execute("UPDATE training_exercises SET name = ? WHERE id = ?", (target_name, source_tex))
        else:
            await db.execute(
                "UPDATE training_entries SET exercise_id = ? WHERE exercise_id = ?", (target_tex, source_tex)
            )
            await db.execute("DELETE FROM training_exercises WHERE id = ?", (source_tex,))
    source_lib = await _library_id(db, source)
    if source_lib is not None:
        await db.execute(
            "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) "
            "SELECT ?, tag FROM exercise_library_tags WHERE library_id = ?",
            (target_lib, source_lib),
        )
        await db.execute("DELETE FROM exercise_library_tags WHERE library_id = ?", (source_lib,))
        await db.execute("DELETE FROM exercise_library WHERE id = ?", (source_lib,))
    await db.execute("INSERT OR IGNORE INTO exercise_aliases (alias, library_id) VALUES (?, ?)", (source, target_lib))


async def up(db: aiosqlite.Connection) -> None:
    equipment_check = ", ".join(f"'{value}'" for value in EQUIPMENT)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS exercise_families (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_order INTEGER NOT NULL DEFAULT 0
        )"""
    )
    if "family_id" not in await _columns(db, "exercise_library"):
        await db.execute(
            "ALTER TABLE exercise_library ADD COLUMN family_id INTEGER REFERENCES exercise_families(id) ON DELETE SET NULL"
        )
    if "default_equipment" not in await _columns(db, "exercise_library"):
        await db.execute(
            "ALTER TABLE exercise_library ADD COLUMN default_equipment TEXT "
            f"CHECK (default_equipment IS NULL OR default_equipment IN ({equipment_check}))"
        )
    if "equipment" not in await _columns(db, "training_entries"):
        await db.execute(
            "ALTER TABLE training_entries ADD COLUMN equipment TEXT "
            f"CHECK (equipment IS NULL OR equipment IN ({equipment_check}))"
        )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS exercise_aliases (
            alias TEXT PRIMARY KEY COLLATE NOCASE CHECK (length(trim(alias)) BETWEEN 1 AND 80),
            library_id INTEGER NOT NULL REFERENCES exercise_library(id) ON DELETE CASCADE
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_exercise_aliases_library ON exercise_aliases(library_id)")

    for order, name in enumerate(FAMILIES):
        await db.execute("INSERT OR IGNORE INTO exercise_families (name, display_order) VALUES (?, ?)", (name, order))
    family_ids = {row[0].lower(): row[1] for row in await db.execute_fetchall("SELECT name, id FROM exercise_families")}

    order_row = await db.execute_fetchall("SELECT COALESCE(MAX(display_order), 0) FROM exercise_library")
    next_order = order_row[0][0] + 1
    for name, section, metric, family, equipment in NEW_MOVEMENTS:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO exercise_library "
            "(section, name, sets, reps, notes, display_order, metric, builtin, archived) "
            "VALUES (?, ?, NULL, '', '', ?, ?, 1, 0)",
            (section, name, next_order, metric),
        )
        if cursor.rowcount:
            next_order += 1
        library_id = await _library_id(db, name)
        await db.execute(
            "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) VALUES (?, 'crossfit')", (library_id,)
        )
        await db.execute(
            "UPDATE exercise_library SET family_id = ?, default_equipment = ? WHERE id = ?",
            (family_ids[family.lower()], equipment, library_id),
        )

    for source, target in MERGES:
        await _merge(db, source, target)

    for name, (family, equipment) in EXISTING.items():
        await db.execute(
            "UPDATE exercise_library SET family_id = ?, default_equipment = ? WHERE lower(name) = lower(?)",
            (family_ids[family.lower()], equipment, name),
        )

    for name, metric in METRIC_FIXES:
        await db.execute("UPDATE exercise_library SET metric = ? WHERE lower(name) = lower(?)", (metric, name))
        await db.execute(
            "UPDATE training_exercises SET metric = ? WHERE lower(name) = lower(?) AND NOT EXISTS "
            "(SELECT 1 FROM training_entries e WHERE e.exercise_id = training_exercises.id AND e.duration IS NOT NULL)",
            (metric, name),
        )

    library_names = {row[0].lower() for row in await db.execute_fetchall("SELECT name FROM exercise_library")}
    for name, aliases in ALIASES.items():
        library_id = await _library_id(db, name)
        if library_id is None:
            continue
        for alias in aliases:
            # An alias that equals a library name would make matching ambiguous.
            if alias.lower() in library_names:
                continue
            await db.execute(
                "INSERT OR IGNORE INTO exercise_aliases (alias, library_id) VALUES (?, ?)", (alias, library_id)
            )
