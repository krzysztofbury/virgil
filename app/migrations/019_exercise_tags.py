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

009 and 016 are deliberately left writing the OLD (`category`) shape — this
migration is the ONE place the category-to-tags conversion happens, on every
install, fresh or upgraded. Giving 009/016 their own copy of the post-019
shape briefly seemed appealing (a fresh install would skip the conversion
entirely) but it means the fresh-install path and the production-upgrade path
run different code, so the conversion this migration performs — the one that
actually runs against the user's real database — would be exercised only by
this file's own unit tests, never by an end-to-end fresh chain. The `category`
guard below exists only for THIS module's own idempotency (`up()` may be
called twice on the same connection; see test_is_idempotent), not to skip a
fresh install.
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
    from app.library_validation import MAX_TAG_LEN, LibraryWriteError, normalize_tag

    if category in _DROPPED_CATEGORIES:
        return None
    if category in _EXPLICIT_TAGS:
        return _EXPLICIT_TAGS[category]
    try:
        return normalize_tag(category)
    except LibraryWriteError as exc:
        if "normalises to nothing" in exc.message:
            # A category of pure punctuation carries no information; drop it
            # rather than aborting the whole migration over one junk row.
            return None
        # Otherwise the normalised tag exceeds MAX_TAG_LEN. Categories were
        # free text capped at 100 characters on both write surfaces (settings
        # and the REST API) — well past the 40-char tag limit — so a long
        # category is real user data, not noise. Truncating the raw input to
        # MAX_TAG_LEN before normalising fits the vast majority of cases:
        # lowercasing, whitespace-to-dash, invalid-char removal and
        # dash-collapse only ever shorten or hold length steady, and so does
        # transliteration for the letters this codebase cares about (Polish
        # ł/Ł and friends fold 1:1). It is NOT an absolute guarantee, though:
        # normalize_tag's transliteration step maps a handful of characters
        # to a *longer* ASCII sequence (æ -> "ae", ß -> "ss"), so a category
        # that is mostly those letters can still exceed MAX_TAG_LEN after
        # truncating the raw input. The except below already covers that —
        # same as the pure-punctuation case above, a category that still
        # doesn't fit gets dropped rather than aborting the whole migration.
        try:
            return normalize_tag(category[:MAX_TAG_LEN])
        except LibraryWriteError:
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

    survivors: dict[str, dict] = {}
    tags_for: dict[str, set[str]] = {}
    for key, group in groups.items():
        explicit = [r for r in group if (r["name"].lower(), r["section"], r["metric"]) in seeded]
        survivor = explicit[0] if explicit else group[0]
        merged = dict(survivor)
        # Stripped for data hygiene (a legacy name with stray whitespace
        # shouldn't propagate) -- NOT for matching `key` later. Tag inserts
        # below are keyed off the INSERT's own lastrowid, not a name lookup,
        # specifically so this stripping (or any other name transform) can
        # never cause a tag-attachment miss.
        merged["name"] = survivor["name"].strip()
        merged["builtin"] = 0 if any(not r["builtin"] for r in group) else 1
        merged["archived"] = 0 if any(not r["archived"] for r in group) else 1
        for field in ("sets", "reps", "notes"):
            if not merged.get(field):
                for r in group:
                    if r.get(field):
                        merged[field] = r[field]
                        break
        survivors[key] = merged
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
            UNIQUE(name COLLATE NOCASE)
        )"""
    )
    # SQLite's lower() (and hence COLLATE NOCASE) is ASCII-only, while `key`
    # was built with Python's Unicode-aware str.lower() -- a name like
    # 'ĆWICZENIE' would satisfy `key == "ćwiczenie"` in Python but NOT
    # `lower(name) = 'ćwiczenie'` in SQL (SQLite's lower() leaves the
    # accented characters untouched). Looking the survivor back up by name
    # would silently miss and drop its tags. Capturing each INSERT's own
    # lastrowid sidesteps the question entirely: no name comparison, no
    # collation, no second place for the two spellings to disagree.
    survivor_ids: dict[str, int] = {}
    for key, s in survivors.items():
        cursor = await db.execute(
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
        survivor_ids[key] = cursor.lastrowid
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
        library_id = survivor_ids[key]
        for tag in sorted(tags):
            await db.execute(
                "INSERT OR IGNORE INTO exercise_library_tags (library_id, tag) VALUES (?, ?)",
                (library_id, tag),
            )
