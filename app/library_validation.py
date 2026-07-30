"""Shared exercise-library field rules.

Both app/routers/settings.py (Settings -> App Config, form-encoded strings) and
app/routers/api.py (REST CRUD for MCP clients, pydantic-typed) write to the same
exercise_library table and must apply identical normalization/validation — a
rule that only one of the two enforces is exactly how library_add/library_update
(settings.py) ended up silently dropping `metric` while api.py wrote it
correctly. A mis-typed metric is not cosmetic: app/exercise_library.py's
CROSSFIT_MOVEMENTS comment documents that a wrong metric='reps' default is the
reason that vocabulary was split out of EXERCISE_LIBRARY in the first place,
and the WOD parser + resolve_movement() copy `metric` straight from this table
into training_exercises, feeding the weekly Total Reps aggregate.
"""

from app.validation import truncate

LIBRARY_METRICS = ("reps", "time")


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
