"""Parse a free-text WOD note into structured training entries.

The user writes one messy line from memory after training; an LLM turns it into
entries. The model is constrained to the movement vocabulary held in
exercise_library — every non-archived row, not just the ones labelled
'CrossFit' — without a closed list it invents 'Thruster', 'thrusters' and
'Thruster 43kg' as three movements and the catalogue rots within a month.

Anything the model returns outside that vocabulary is reported as unmatched
rather than created, so the user resolves it on the confirmation screen.
"""

import logging
from dataclasses import dataclass, field

from app.services.llm import call_llm, parse_andy_response

logger = logging.getLogger(__name__)

MAX_WOD_CHARS = 4000

# The vocabulary is concatenated whole into the system prompt (see
# parse_wod below) — with no LIMIT and no count check, it grows unbounded:
# POST /api/library is agent-callable (an agent looping on add_exercise is
# exactly what an MCP-writable dictionary invites), and every extra row is
# paid for on every subsequent parse until the prompt breaks the model's
# context window and the feature degrades to permanent parse_error. The
# user has 77 non-archived exercise_library rows today, 73 once the 4
# same-name duplicates across categories collapse; 500 is a generous
# ceiling that still fails loudly well before that happens.
MAX_LIBRARY_MOVEMENTS = 500

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
    """The closed vocabulary: every active exercise_library row, deduped by name.

    Category organises the Settings picker; it does not gate what the parser
    may recognise. exercise_library as a whole — Warmup, Stretching, Gym
    classics, Kettlebell, ... not just CrossFit — is the user's curated
    dictionary, and a warm-up or a barbell lift the user actually logs
    deserves the same closed-vocabulary treatment a CrossFit movement gets.

    UNIQUE on exercise_library is (category, name), not (name) — a name can
    exist under two categories. Four do today: Back Squat, Bench Press,
    Deadlift (Gym classics + CrossFit) and Pull-up (Workout B + CrossFit).
    Both rows happen to be (section, metric)-identical today, but the
    vocabulary must not depend on that being true forever, so a duplicate is
    resolved by an explicit, deterministic rule rather than "whichever the
    query happened to return first": prefer the CrossFit row; otherwise the
    lowest display_order. CrossFit rows carry a `metric` seeded explicitly
    (migration 016); older rows had theirs derived by migration 011 from the
    rep-spec string — if a future duplicate ever disagrees (say a `Row`
    seeded metric='time' under CrossFit and another `Row` derived as 'reps'
    elsewhere), picking arbitrarily would silently mis-type the movement and
    feed erg strokes into the weekly rep count. The ORDER BY below sorts a
    duplicate's CrossFit row first and breaks remaining ties by
    display_order, so keeping only the first row seen per lower(name)
    implements the tie-break. resolve_movement() in wod_movements.py uses the
    identical ORDER BY — both must agree, or a movement resolves to a
    different section/metric depending on which one is asked.
    """
    rows = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library "
        "WHERE archived = 0 "
        "ORDER BY (category != 'CrossFit'), display_order"
    )
    seen: set[str] = set()
    movements: list[dict] = []
    for r in rows:
        row = dict(r)
        key = row["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        movements.append(row)

    assert len(movements) <= MAX_LIBRARY_MOVEMENTS, (
        f"movement vocabulary has grown to {len(movements)} rows "
        f"(max {MAX_LIBRARY_MOVEMENTS}) — prune exercise_library before the WOD "
        "parser's system prompt grows any further"
    )
    return movements


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

    raw_entries = payload.get("entries")
    raw_unmatched = payload.get("unmatched")
    entries: list[ParsedEntry] = []
    unmatched: list[str] = [str(u) for u in raw_unmatched] if isinstance(raw_unmatched, list) else []

    for item in raw_entries if isinstance(raw_entries, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("movement") or "").strip()
        canonical = by_lower.get(name.lower())
        if not canonical:
            if name and name not in unmatched:
                unmatched.append(name)
            continue
        try:
            # Sets are 1-indexed. A missing value means a single set; a parsed
            # value below 1 is nonsense from the model — clamp it, but say so,
            # because silently collapsing set 0 and set 1 corrupts the workout
            # rather than crashing, which is the failure that hides longest.
            parsed_set = _coerce(item.get("set_number"), int)
            if parsed_set is None:
                parsed_set = 1
            elif parsed_set < 1:
                logger.warning("WOD parser clamped set_number %r to 1 for %r", parsed_set, canonical)
                parsed_set = 1
            entry = ParsedEntry(
                movement=canonical,
                set_number=parsed_set,
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
