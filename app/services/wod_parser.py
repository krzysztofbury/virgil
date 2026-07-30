"""Parse a free-text WOD note into structured training entries.

The user writes one messy line from memory after training; an LLM turns it into
entries. The model is constrained to the CrossFit movement vocabulary held in
exercise_library — without a closed list it invents 'Thruster', 'thrusters' and
'Thruster 43kg' as three movements and the catalogue rots within a month.

Anything the model returns outside that vocabulary is reported as unmatched
rather than created, so the user resolves it on the confirmation screen.
"""

import logging
from dataclasses import dataclass, field

from app.services.llm import call_llm, parse_andy_response

logger = logging.getLogger(__name__)

MAX_WOD_CHARS = 4000

_SYSTEM_PROMPT = """You extract structured training data from a short, messy \
note a CrossFit athlete wrote from memory after a session. The note may be in \
Polish or English and may mix both.

Return ONLY a JSON object of this exact shape:
{"entries": [{"movement": str, "set_number": int, "reps": int|null,
 "weight": float|null, "duration": float|null, "note": str}], "unmatched": [str]}

Rules:
- "movement" MUST be copied verbatim from the allowed movement list below. \
Never invent a movement name and never translate one.
- Any movement in the note that is not in the list goes into "unmatched" as the \
user wrote it. Do not guess a near match.
- Rep schemes such as "21-15-9" produce one entry per round, set_number 1,2,3, \
with reps 21, 15, 9 and the scheme copied into "note".
- "5x5 @70" produces 5 entries, set_number 1..5, reps 5, weight 70.
- "weight" is kilograms. "duration" is seconds.
- A metcon result (a finishing time, or rounds+reps for an AMRAP) goes into the \
"note" of that movement's first entry. Do not invent an entry for it.
- Omit nothing you can map; guess nothing you cannot.

Allowed movements:
"""


@dataclass(frozen=True)
class ParsedEntry:
    movement: str
    set_number: int
    reps: int | None
    weight: float | None
    duration: float | None
    note: str


@dataclass(frozen=True)
class ParsedWod:
    entries: list[ParsedEntry] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)


async def canonical_movements(db) -> list[dict]:
    """The closed vocabulary: active CrossFit rows from the exercise library."""
    rows = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library "
        "WHERE category = 'CrossFit' AND archived = 0 ORDER BY display_order"
    )
    return [dict(r) for r in rows]


def _coerce(value, caster):
    if value is None:
        return None
    try:
        return caster(value)
    except (TypeError, ValueError):
        raise


async def parse_wod(db, text: str) -> ParsedWod:
    """Turn a free-text WOD note into entries constrained to the library."""
    if not text or not text.strip():
        raise ValueError("WOD text is empty")

    text = text.strip()[:MAX_WOD_CHARS]
    movements = await canonical_movements(db)
    by_lower = {m["name"].lower(): m["name"] for m in movements}

    system_prompt = _SYSTEM_PROMPT + "\n".join(f"- {m['name']}" for m in movements)
    raw = await call_llm(db, system_prompt, text, json_mode=True, max_tokens=4096)
    payload = parse_andy_response(raw)

    entries: list[ParsedEntry] = []
    unmatched: list[str] = [str(u) for u in payload.get("unmatched") or []]

    for item in payload.get("entries") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("movement") or "").strip()
        canonical = by_lower.get(name.lower())
        if not canonical:
            if name and name not in unmatched:
                unmatched.append(name)
            continue
        try:
            entry = ParsedEntry(
                movement=canonical,
                set_number=_coerce(item.get("set_number"), int) or 1,
                reps=_coerce(item.get("reps"), int),
                weight=_coerce(item.get("weight"), float),
                duration=_coerce(item.get("duration"), float),
                note=str(item.get("note") or "")[:200],
            )
        except (TypeError, ValueError):
            # One malformed row must not lose the rest of the workout.
            logger.warning("WOD parser skipped malformed entry: %r", item)
            continue
        entries.append(entry)

    return ParsedWod(entries=entries, unmatched=unmatched)
