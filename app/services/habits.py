"""Freedom: the habits a person is breaking free from.

One module for any of them - porn, alcohol, nicotine, gambling, or one the
person names. Each habit carries its own definition of a slip, so "I watched"
and "I drank" are the same check-in with different words, and its own start
date, target and "why". Day logs, bricks and relapse markers hang off a habit
(feniks_daily, feniks_bricks, pmo_events keep their historical table names).

A habit the person stops tracking keeps its history (tracked = 0); tracking it
again resumes where it left off.
"""

import re
import unicodedata
from datetime import date as calendar_date

NAME_MAX = 40
LABEL_MAX = 30
DEFINITION_MAX = 600
UNIT_MAX = 20
THRESHOLD_MAX = 1440
TARGET_MAX = 3650
WHY_MAX = 2000
WEEKLY_TARGET_PCT = 75

# The catalog offered in Settings and on the Freedom page. A slip is a day with
# at least `slip_threshold` of `amount_unit`; a habit without a unit has no
# amount to ask, and one without a near-miss label has no near miss to record.
PRESETS = (
    {
        "slug": "porn",
        "name": "Porn",
        "slip_label": "I watched",
        "amount_unit": "minutes",
        "slip_threshold": 5,
        "near_miss_label": "Edging",
        "slip_definition": (
            "Watched means at least 5 total minutes of intentional viewing in one day. "
            "A shorter exposure stays a clean outcome; record it in the day note."
        ),
    },
    {
        "slug": "alcohol",
        "name": "Alcohol",
        "slip_label": "I drank",
        "amount_unit": "drinks",
        "slip_threshold": 1,
        "near_miss_label": "",
        "slip_definition": "A slip is any alcoholic drink. Alcohol-free versions stay clean.",
    },
    {
        "slug": "nicotine",
        "name": "Nicotine",
        "slip_label": "I smoked",
        "amount_unit": "",
        "slip_threshold": None,
        "near_miss_label": "",
        "slip_definition": "A slip is any cigarette, vape or pouch.",
    },
    {
        "slug": "cannabis",
        "name": "Cannabis",
        "slip_label": "I used",
        "amount_unit": "",
        "slip_threshold": None,
        "near_miss_label": "",
        "slip_definition": "A slip is any use, in any form.",
    },
    {
        "slug": "drugs",
        "name": "Drugs",
        "slip_label": "I used",
        "amount_unit": "",
        "slip_threshold": None,
        "near_miss_label": "",
        "slip_definition": "A slip is any use of a substance you are staying away from.",
    },
    {
        "slug": "gambling",
        "name": "Gambling",
        "slip_label": "I gambled",
        "amount_unit": "",
        "slip_threshold": None,
        "near_miss_label": "",
        "slip_definition": "A slip is any bet or game played for money, online or not.",
    },
    {
        "slug": "social-media",
        "name": "Social media",
        "slip_label": "I scrolled",
        "amount_unit": "minutes",
        "slip_threshold": 30,
        "near_miss_label": "",
        "slip_definition": "A slip is 30 or more minutes of feeds in one day. Messaging people stays clean.",
    },
    {
        "slug": "sugar",
        "name": "Sugar",
        "slip_label": "I had sugar",
        "amount_unit": "",
        "slip_threshold": None,
        "near_miss_label": "",
        "slip_definition": "A slip is any sweets, dessert or sugary drink.",
    },
)
PRESETS_BY_SLUG = {preset["slug"]: preset for preset in PRESETS}

EDITABLE = (
    "name",
    "slip_label",
    "slip_definition",
    "amount_unit",
    "slip_threshold",
    "near_miss_label",
    "start_date",
    "target_days",
    "big_why",
)


class HabitError(ValueError):
    pass


def _today() -> str:
    return calendar_date.today().isoformat()


# Letters NFKD does not split into an ASCII base and a combining mark.
_ASCII_FOLDS = str.maketrans({"ł": "l", "đ": "d", "ø": "o", "ħ": "h", "ı": "i", "ß": "ss", "æ": "ae", "œ": "oe"})


def slugify(name: str) -> str:
    """URL-safe ASCII for a habit name: "Słodycze" becomes "slodycze".

    A name with no Latin letters at all (Cyrillic, Greek) becomes "habit";
    add_custom then numbers it, so two such habits never share a slug.
    """
    folded = unicodedata.normalize("NFKD", name.lower().translate(_ASCII_FOLDS))
    ascii_name = "".join(char for char in folded if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    return slug[:NAME_MAX] or "habit"


async def list_habits(db, *, tracked_only: bool = True) -> list[dict]:
    where = "WHERE tracked = 1 " if tracked_only else ""
    rows = await db.execute_fetchall(f"SELECT * FROM habits {where}ORDER BY position, id")
    return [dict(row) for row in rows]


async def get_habit(db, slug: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM habits WHERE slug = ?", (slug,))
    return dict(rows[0]) if rows else None


async def resolve_habit(db, slug: str | None = None) -> dict:
    """The named habit, or the first one being tracked when no name is given.

    The default keeps single-habit callers (older agents, the REST API without a
    `habit` parameter) working unchanged.
    """
    if slug:
        habit = await get_habit(db, slug)
        if habit is None:
            raise LookupError(f"No habit named {slug!r}.")
        return habit
    tracked = await list_habits(db)
    if not tracked:
        raise LookupError("No habit is being tracked. Choose one in Settings, Freedom.")
    return tracked[0]


async def _next_position(db) -> int:
    rows = await db.execute_fetchall("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM habits")
    return rows[0]["p"]


async def track(db, slug: str) -> dict:
    """Start (or resume) tracking a catalog habit or an existing custom one."""
    existing = await get_habit(db, slug)
    if existing is not None:
        await db.execute("UPDATE habits SET tracked = 1 WHERE id = ?", (existing["id"],))
        await db.commit()
        return {**existing, "tracked": 1}
    preset = PRESETS_BY_SLUG.get(slug)
    if preset is None:
        raise LookupError(f"No habit named {slug!r}.")
    await _insert(db, preset)
    return await get_habit(db, slug)


async def add_custom(db, name: str) -> dict:
    """Track a habit the catalog does not offer, under the person's own name for it."""
    name = (name or "").strip()[:NAME_MAX]
    if not name:
        raise HabitError("Name the habit you want to break.")
    # "Alcohol" typed by hand is the catalog's Alcohol, and a name already in use
    # resumes that habit: either way its history stays in one place.
    slug = slugify(name)
    if slug in PRESETS_BY_SLUG:
        return await track(db, slug)
    habits = await list_habits(db, tracked_only=False)
    same_name = next((habit for habit in habits if _name_key(habit["name"]) == _name_key(name)), None)
    if same_name is not None:
        return await track(db, same_name["slug"])
    # A different name that folds to a slug in use gets its own: "habit-2", "habit-3".
    taken = {habit["slug"] for habit in habits} | set(PRESETS_BY_SLUG)
    base, suffix = slug, 2
    while slug in taken:
        slug, suffix = f"{base}-{suffix}", suffix + 1
    await _insert(
        db,
        {
            "slug": slug,
            "name": name,
            "slip_label": "I slipped",
            "amount_unit": "",
            "slip_threshold": None,
            "near_miss_label": "",
            "slip_definition": "",
        },
    )
    return await get_habit(db, slug)


def _name_key(name: str) -> str:
    """Case and punctuation do not make a new habit: "Coffee!" is "coffee". Letters
    in any script count, so two Cyrillic names stay two habits."""
    return re.sub(r"[\W_]+", "", name.casefold())


async def _insert(db, spec: dict) -> None:
    await db.execute(
        "INSERT INTO habits (slug, name, slip_label, slip_definition, amount_unit, slip_threshold, "
        "near_miss_label, start_date, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            spec["slug"],
            spec["name"],
            spec["slip_label"],
            spec["slip_definition"],
            spec["amount_unit"],
            spec["slip_threshold"],
            spec["near_miss_label"],
            _today(),
            await _next_position(db),
        ),
    )
    await db.commit()


async def untrack(db, slug: str) -> None:
    """Stop tracking; the history stays and returns when the habit is tracked again."""
    await db.execute("UPDATE habits SET tracked = 0 WHERE slug = ?", (slug,))
    await db.commit()


async def sync_tracked(db, slugs: set[str]) -> None:
    """Make exactly `slugs` the tracked set (the Settings checklist)."""
    for habit in await list_habits(db, tracked_only=False):
        if habit["slug"] not in slugs and habit["tracked"]:
            await untrack(db, habit["slug"])
    for slug in slugs:
        if slug in PRESETS_BY_SLUG or await get_habit(db, slug) is not None:
            await track(db, slug)


def _text(changes: dict, key: str, limit: int, *, required: bool = False) -> str:
    value = (changes[key] or "").strip()
    if required and not value:
        raise HabitError(f"{key.replace('_', ' ').capitalize()} cannot be empty.")
    return value[:limit]


async def update_habit(db, slug: str, changes: dict) -> dict:
    """Change only the supplied fields of one habit."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise HabitError(f"Unknown habit fields: {', '.join(sorted(unknown))}")
    if db.in_transaction:
        raise RuntimeError("Habit writes require a connection without pending writes")
    # Read, merge and write in one transaction: two concurrent edits of different
    # fields would otherwise each write back the other's old value.
    await db.execute("BEGIN IMMEDIATE")
    try:
        habit = await get_habit(db, slug)
        if habit is None:
            raise LookupError(f"No habit named {slug!r}.")
        _apply_changes(habit, changes)
        await db.execute(
            f"UPDATE habits SET {', '.join(f'{field} = ?' for field in EDITABLE)} WHERE id = ?",
            (*(habit[field] for field in EDITABLE), habit["id"]),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return habit


def _apply_changes(habit: dict, changes: dict) -> None:
    """Validate the supplied fields and set them on `habit` in place."""
    if "name" in changes:
        habit["name"] = _text(changes, "name", NAME_MAX, required=True)
    if "slip_label" in changes:
        habit["slip_label"] = _text(changes, "slip_label", LABEL_MAX, required=True)
    if "slip_definition" in changes:
        habit["slip_definition"] = _text(changes, "slip_definition", DEFINITION_MAX)
    if "amount_unit" in changes:
        habit["amount_unit"] = _text(changes, "amount_unit", UNIT_MAX)
    if "near_miss_label" in changes:
        habit["near_miss_label"] = _text(changes, "near_miss_label", LABEL_MAX)
    if "slip_threshold" in changes:
        threshold = changes["slip_threshold"]
        if threshold is not None and (
            isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= THRESHOLD_MAX
        ):
            raise HabitError(f"The slip threshold must be between 1 and {THRESHOLD_MAX}, or empty.")
        habit["slip_threshold"] = threshold
    if not habit["amount_unit"]:
        habit["slip_threshold"] = None
    if "start_date" in changes:
        try:
            start = calendar_date.fromisoformat(changes["start_date"] or "")
        except ValueError as exc:
            raise HabitError("The start date must be a date.") from exc
        if start > calendar_date.today():
            raise HabitError("The start date must be today or earlier.")
        habit["start_date"] = start.isoformat()
    if "target_days" in changes:
        target = changes["target_days"]
        if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= TARGET_MAX:
            raise HabitError(f"The target must be between 1 and {TARGET_MAX} days.")
        habit["target_days"] = target
    if "big_why" in changes:
        habit["big_why"] = (changes["big_why"] or "")[:WHY_MAX]
