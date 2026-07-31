"""Shared exercise-library field rules AND write policy.

Both app/routers/settings.py (Settings -> App Config, form-encoded strings) and
app/routers/api.py (REST CRUD for MCP clients, pydantic-typed) write to the same
exercise_library table and must make identical accept/reject decisions — a
prior pass shared normalization (normalize_library_text, clamp_library_sets,
valid_library_metric) but not policy: settings.py silently coerced an invalid
section/metric to a default and let INSERT/UPDATE OR IGNORE swallow a
duplicate name, a rename collision, or a builtin edit, while api.py raised
loudly for every one of those. Two surfaces reaching different decisions on
identical input is the defect (not which decision "feels" nicer), so
`validate_library_write` below is now the ONE place the decision gets made.
Both routers call it and differ only in how they render a rejection:
settings.py redirects with `?err=`, api.py raises
HTTPException(exc.status, exc.message).

I2 (2026-07-30 review): renaming a library row while `training_exercises`
still holds entries under the old name would silently split that movement's
history in two — app/services/wod_movements.py's resolve_movement() matches
training_exercises by name, so the next WOD mentioning the new name creates a
SECOND row instead of reusing the old one, and Personal Bests (which groups by
tex.id) ends up showing half the progression on each. `validate_library_write`
refuses a name change when training_exercises has a matching row under the old
name, for both surfaces.
"""

import re

from app.validation import truncate

LIBRARY_METRICS = ("reps", "time")
LIBRARY_SECTIONS = ("Warmup", "Core", "Cardio", "Stretching")

MAX_TAG_LEN = 40

# Free-form tags with normalisation on write. The category field this replaces
# was raw free text with a datalist, which is how a program label ("Workout A
# (KB full-body)") became a category. Normalising here means "Kettlebell",
# "KETTLEBELL" and "Kettle Bell " are one tag rather than three.
_TAG_INVALID_CHARS = re.compile(r"[^a-z0-9-]+")
_TAG_DASHES = re.compile(r"-{2,}")


def normalize_library_text(value: str, max_len: int) -> str:
    """Strip surrounding whitespace, then clamp to max_len.

    Both routers must apply this identically to name/reps/notes —
    api.py previously truncated `reps`/`notes` without stripping first, which
    diverged from settings.py's form handlers.
    """
    return truncate((value or "").strip(), max_len)


def clamp_library_sets(sets: int | None) -> int | None:
    """A small human rep count, not a free integer — 20 sets in one exercise
    is already an outlier."""
    return max(1, min(20, sets)) if sets is not None else None


def valid_library_metric(metric: str) -> bool:
    return metric in LIBRARY_METRICS


class LibraryWriteError(Exception):
    """A library write that must be refused, not silently coerced or dropped.

    `status` is the HTTP status both surfaces agree this failure deserves
    (422 for a bad value, 409 for a conflict) — api.py raises it directly as
    HTTPException(status, message); settings.py has no status code to hand
    the browser on a redirect, so it carries the same message through
    `?err=` instead. The point is that both surfaces reach this exception,
    or don't, together, on the same input.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def normalize_tag(raw: str) -> str:
    """Lowercase, whitespace-to-dash, alphanumerics and dashes only.

    Raises LibraryWriteError(422) when nothing survives — a tag that
    normalises to the empty string is a typo, not an unnamed tag.
    """
    text = (raw or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = _TAG_INVALID_CHARS.sub("", text)
    text = _TAG_DASHES.sub("-", text).strip("-")
    if not text:
        raise LibraryWriteError(422, f"tag {raw!r} normalises to nothing")
    if len(text) > MAX_TAG_LEN:
        raise LibraryWriteError(422, f"tag {text!r} exceeds {MAX_TAG_LEN} characters")
    return text


def normalize_tags(raw: list[str] | str | None) -> list[str]:
    """Normalise a list (or comma-separated string) of tags.

    Blank items are dropped silently — a trailing comma in a form field is not
    a user error. A non-blank item that normalises to nothing still raises.
    """
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    # Use dict instead of set to preserve insertion order (Python 3.7+) and
    # deduplicate by key. This makes sorted() essential — without it, the
    # output order becomes dependent on input order rather than alphabetical,
    # which tests can then verify deterministically. A set would leave the
    # mutation `sorted(out)` → `list(out)` undetectable on some random seeds.
    out: dict[str, None] = {}
    for item in items:
        if not str(item).strip():
            continue
        out[normalize_tag(str(item))] = None
    return sorted(out)


async def _name_taken(db, name: str, exclude_id: int | None = None) -> bool:
    """Case-insensitive, Unicode-aware name collision check.

    exercise_library.name is UNIQUE(name COLLATE NOCASE) (migration 019), but
    SQLite's COLLATE NOCASE — like its lower() function — only folds ASCII:
    'ĆWICZENIE' and 'ćwiczenie' both satisfy that constraint as distinct rows
    (SQL lower('ĆWICZENIE') is 'Ćwiczenie' — only the ASCII letters fold; the
    leading Ć does not). Comparing in Python (str.lower(), fully
    Unicode-aware) instead of delegating to SQL catches what the database
    constraint cannot; the DB constraint remains a backstop for the ASCII
    case (and for any writer that bypasses this function entirely).
    """
    sql = "SELECT name FROM exercise_library"
    params: tuple = ()
    if exclude_id is not None:
        sql += " WHERE id != ?"
        params = (exclude_id,)
    rows = await db.execute_fetchall(sql, params)
    target = name.lower()
    return any(r["name"].lower() == target for r in rows)


async def _training_history_exists_for(db, name: str) -> bool:
    """Case-insensitive, Unicode-aware check for training_exercises history
    under `name` — same rationale as _name_taken(), same fix: SQL lower() is
    ASCII-only, so a rename onto a non-ASCII case-variant of a name that
    already has logged history (e.g. a Polish movement) would slip past a
    `lower(name) = lower(?)` SQL comparison. This backs the I2 guard, whose
    entire purpose is to catch exactly that rename before it splits the
    movement's Personal Best/volume history across two rows.
    """
    rows = await db.execute_fetchall("SELECT name FROM training_exercises")
    target = name.lower()
    return any(r["name"].lower() == target for r in rows)


async def validate_library_write(
    db,
    *,
    op: str,
    entry_id: int | None = None,
    existing: dict | None = None,
    fields: dict | None = None,
) -> dict | None:
    """The one write policy both surfaces share.

    op="create": `fields["name"]` is required. `fields` may omit
    section/sets/reps/notes/metric, which then take the same defaults both
    surfaces have always used (section='Core', metric='reps', sets=None,
    reps/notes=''). Returns the full row to INSERT.

    op="update": `existing` is the current DB row (a dict, e.g. from
    `dict(row)`). `fields` holds ONLY the keys the caller wants to change —
    a key's absence means "leave this column unchanged", never "reset it to
    a default". This is what lets a request that omits `metric` (a stale
    cached settings form, or a partial PATCH) leave the column alone instead
    of silently rewriting it to the Form/pydantic default. Returns the
    subset of columns to SET (possibly empty if `fields` was empty).

    op="delete": `existing` is the current DB row. Returns None.

    Raises LibraryWriteError for anything that must be refused outright: an
    invalid section/metric, a blank required name, a duplicate name, a rename
    that collides with another row or with training_exercises history (I2),
    or any edit/delete of a builtin row other than `archived`.

    Callers are responsible for the 404 case (fetch the row first; if it
    doesn't exist, respond before calling this at all) — this function only
    ever sees a row that's actually there.
    """
    fields = dict(fields) if fields else {}

    if op == "delete":
        assert existing is not None, "delete requires the current row"
        if existing["builtin"]:
            raise LibraryWriteError(409, f"library entry {entry_id} is builtin — archive it instead")
        return None

    if op == "create":
        name = normalize_library_text(fields.get("name", ""), 100)
        if not name:
            raise LibraryWriteError(422, "name is required")

        section = fields.get("section", "Core")
        if section not in LIBRARY_SECTIONS:
            raise LibraryWriteError(422, f"section must be one of {'/'.join(LIBRARY_SECTIONS)}, got {section!r}")

        metric = fields.get("metric", "reps")
        if not valid_library_metric(metric):
            raise LibraryWriteError(422, f"metric must be one of {'/'.join(LIBRARY_METRICS)}, got {metric!r}")

        if await _name_taken(db, name):
            raise LibraryWriteError(409, f"{name!r} already exists")

        return {
            "section": section,
            "name": name,
            "sets": clamp_library_sets(fields.get("sets")),
            "reps": normalize_library_text(fields.get("reps", ""), 100),
            "notes": normalize_library_text(fields.get("notes", ""), 300),
            "metric": metric,
        }

    if op == "update":
        assert existing is not None, "update requires the current row"
        if existing["builtin"] and set(fields) - {"archived"}:
            raise LibraryWriteError(409, f"library entry {entry_id} is builtin — only 'archived' can change")

        result: dict = {}

        if "name" in fields:
            name = normalize_library_text(fields["name"], 100)
            if not name:
                raise LibraryWriteError(422, "name cannot be blank")
            if name.lower() != existing["name"].lower():
                if await _name_taken(db, name, exclude_id=entry_id):
                    raise LibraryWriteError(409, f"{name!r} already exists")
                # I2: a rename must not orphan training_exercises rows still
                # holding history under the OLD name — resolve_movement()
                # matches training_exercises by name, so the next WOD
                # mentioning the new name would create a SECOND row and
                # split the movement's Personal Best/volume history in two.
                if await _training_history_exists_for(db, existing["name"]):
                    raise LibraryWriteError(
                        409,
                        f"cannot rename {existing['name']!r} — training history exists under "
                        "that name; archive this entry and create a new one instead",
                    )
            result["name"] = name

        if "section" in fields:
            section = fields["section"]
            if section not in LIBRARY_SECTIONS:
                raise LibraryWriteError(422, f"section must be one of {'/'.join(LIBRARY_SECTIONS)}, got {section!r}")
            result["section"] = section

        if "metric" in fields:
            metric = fields["metric"]
            if not valid_library_metric(metric):
                raise LibraryWriteError(422, f"metric must be one of {'/'.join(LIBRARY_METRICS)}, got {metric!r}")
            result["metric"] = metric

        if "sets" in fields:
            result["sets"] = clamp_library_sets(fields["sets"])

        if "reps" in fields:
            result["reps"] = normalize_library_text(fields["reps"], 100)

        if "notes" in fields:
            result["notes"] = normalize_library_text(fields["notes"], 300)

        if "archived" in fields:
            result["archived"] = 1 if fields["archived"] else 0

        return result

    raise ValueError(f"unknown op {op!r}")
