"""Replace exercise_library.category with tags; make names unique.

`category` was doing three unrelated jobs: echoing `section` (Warmup /
Stretching / Cardio), labelling a training style (CrossFit / Kettlebell /
Gym classics / Bodyweight) and naming a program (Workout A/B). Only the
middle one is information `section` does not already carry, so only that one
becomes a tag. Program labels are dropped — most of those movements are
kettlebell work but not all, and writing a plausible-but-partly-false tag onto
twenty rows is worse than leaving them for the user to label.

The mapping is NOT limited to the categories that ship in the seed data: rows
added through Settings or the REST API since deployment carry arbitrary
categories, and this migration runs against that database, not this repo's.

Merging same-name rows prefers the row whose (name, section, metric) matches
CROSSFIT_MOVEMENTS, because migration 016 wrote `metric` explicitly while rows
seeded by 009 had theirs DERIVED by 011 from the rep-spec string. Preferring
the lowest display_order instead would systematically pick the derived row
(009 seeded 0-45, 016 seeded 46-76) and could silently flip a movement's type.

On a fresh install, 009 and 016 already write the post-019 shape from the
start (no `category` column at all) — the guard below makes this a no-op in
that case, since there is nothing left to migrate.
"""

import aiosqlite

# Categories that merely restate `section`, plus the two program labels.
_DROPPED_CATEGORIES = {
    "Warmup",
    "Stretching",
    "Cardio",
    "Workout A (KB full-body)",
    "Workout B (KB full-body)",
}
_EXPLICIT_TAGS = {
    "CrossFit": "crossfit",
    "Kettlebell": "kettlebell",
    "Gym classics": "gym-classic",
    "Bodyweight": "bodyweight",
}


def _tag_for(category: str) -> str | None:
    from app.library_validation import LibraryWriteError, normalize_tag

    if category in _DROPPED_CATEGORIES:
        return None
    if category in _EXPLICIT_TAGS:
        return _EXPLICIT_TAGS[category]
    try:
        return normalize_tag(category)
    except LibraryWriteError:
        # A category of pure punctuation carries no information; drop it
        # rather than aborting the whole migration over one junk row.
        return None


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
    if not any(c["name"] == "category" for c in cols):
        return  # already migrated

    from app.exercise_library import CROSSFIT_MOVEMENTS

    seeded = {(m["name"].lower(), m["section"], m["metric"]) for m in CROSSFIT_MOVEMENTS}

    rows = [dict(r) for r in await db.execute_fetchall("SELECT * FROM exercise_library ORDER BY display_order, id")]

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["name"].strip().lower(), []).append(r)

    survivors: list[dict] = []
    tags_for: dict[str, set[str]] = {}
    for key, group in groups.items():
        explicit = [r for r in group if (r["name"].lower(), r["section"], r["metric"]) in seeded]
        survivor = explicit[0] if explicit else group[0]
        merged = dict(survivor)
        merged["builtin"] = 0 if any(not r["builtin"] for r in group) else 1
        merged["archived"] = 0 if any(not r["archived"] for r in group) else 1
        for field in ("sets", "reps", "notes"):
            if not merged.get(field):
                for r in group:
                    if r.get(field):
                        merged[field] = r[field]
                        break
        survivors.append(merged)
        tags = {t for t in (_tag_for(r["category"]) for r in group) if t}
        tags_for[key] = tags

    await db.execute(
        """CREATE TABLE exercise_library_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            sets INTEGER,
            reps TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            builtin INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name)
        )"""
    )
    for s in survivors:
        await db.execute(
            "INSERT INTO exercise_library_new "
            "(section, name, sets, reps, notes, display_order, metric, builtin, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["section"],
                s["name"],
                s["sets"],
                s["reps"],
                s["notes"],
                s["display_order"],
                s["metric"],
                s["builtin"],
                s["archived"],
            ),
        )
    await db.execute("DROP TABLE exercise_library")
    await db.execute("ALTER TABLE exercise_library_new RENAME TO exercise_library")

    await db.execute(
        """CREATE TABLE IF NOT EXISTS exercise_library_tags (
            library_id INTEGER NOT NULL REFERENCES exercise_library(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (library_id, tag)
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_library_tags_tag ON exercise_library_tags(tag)")

    for key, tags in tags_for.items():
        if not tags:
            continue
        found = await db.execute_fetchall("SELECT id FROM exercise_library WHERE lower(name) = ? LIMIT 1", (key,))
        if not found:
            continue
        for tag in sorted(tags):
            await db.execute(
                "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) VALUES (?, ?)",
                (found[0]["id"], tag),
            )
