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
"""

from app.validation import truncate

LIBRARY_METRICS = ("reps", "time")
LIBRARY_SECTIONS = ("Warmup", "Core", "Cardio", "Stretching")


def normalize_library_text(value: str, max_len: int) -> str:
    """Strip surrounding whitespace, then clamp to max_len.

    Both routers must apply this identically to name/category/reps/notes —
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


async def validate_library_write(
    db,
    *,
    op: str,
    entry_id: int | None = None,
    existing: dict | None = None,
    category: str | None = None,
    fields: dict | None = None,
) -> dict | None:
    """The one write policy both surfaces share.

    op="create": `category` and `fields["name"]` are required. `fields` may
    omit section/sets/reps/notes/metric, which then take the same defaults
    both surfaces have always used (section='Core', metric='reps', sets=None,
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
    invalid section/metric, a blank required name, a duplicate
    (category, name), a rename that collides with another row, or any
    edit/delete of a builtin row other than `archived`.

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
        norm_category = normalize_library_text(category or "", 100)
        name = normalize_library_text(fields.get("name", ""), 100)
        if not norm_category or not name:
            raise LibraryWriteError(422, "category and name are required")

        section = fields.get("section", "Core")
        if section not in LIBRARY_SECTIONS:
            raise LibraryWriteError(422, f"section must be one of {'/'.join(LIBRARY_SECTIONS)}, got {section!r}")

        metric = fields.get("metric", "reps")
        if not valid_library_metric(metric):
            raise LibraryWriteError(422, f"metric must be one of {'/'.join(LIBRARY_METRICS)}, got {metric!r}")

        dup = await db.execute_fetchall(
            "SELECT id FROM exercise_library WHERE category = ? AND name = ?", (norm_category, name)
        )
        if dup:
            raise LibraryWriteError(409, f"{name!r} already exists in category {norm_category!r}")

        return {
            "category": norm_category,
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
                clash = await db.execute_fetchall(
                    "SELECT id FROM exercise_library WHERE category = ? AND name = ? AND id != ?",
                    (existing["category"], name, entry_id),
                )
                if clash:
                    raise LibraryWriteError(409, f"{name!r} already exists in category {existing['category']!r}")
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
