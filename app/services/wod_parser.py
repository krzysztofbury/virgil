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
# user has 73 non-archived exercise_library rows today (77 before migration
# 019 merged the 4 same-name duplicates that used to exist across
# categories); 500 is a generous ceiling that still fails loudly well
# before that happens.
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

    Tags organise the Settings picker; they do not gate what the parser may
    recognise. exercise_library as a whole — Warmup, Stretching, Gym
    classics, Kettlebell, ... not just CrossFit — is the user's curated
    dictionary, and a warm-up or a barbell lift the user actually logs
    deserves the same closed-vocabulary treatment a CrossFit movement gets.

    exercise_library.name is UNIQUE(name COLLATE NOCASE) (migration 019), but
    that constraint — like SQLite's lower() it's built on — only folds ASCII
    case: 'ĆWICZENIE' and 'ćwiczenie' both satisfy it as distinct rows (SQL
    lower('ĆWICZENIE') is 'Ćwiczenie' — only the ASCII letters fold; the
    leading Ć does not). validate_library_write's own dup checks close that
    gap by comparing in Python (str.lower(), fully Unicode-aware) instead of
    delegating to SQL, so a write through either surface is refused before
    any duplicate — ASCII or not — reaches the table. It used to be possible
    for a name to exist twice under two categories (Back Squat, Bench Press,
    Deadlift and Pull-up all did, briefly, before migration 019 merged them);
    that is no longer reachable through the app, but the
    dedupe-by-first-seen-name below stays as defense in depth against a
    hand-edited or otherwise malformed database: display_order alone is the
    tie-break, should a duplicate ever appear. This function is the one place
    that needs it: the SELECT above has no WHERE on name, so two rows that
    differ only in a non-ASCII letter's case both come back from the same
    query and must be collapsed here. resolve_movement() in wod_movements.py
    no longer carries the equivalent machinery — its WHERE targets one
    specific name, and lower(name) = lower(?) compares both sides with the
    same SQL lower() this table's UNIQUE constraint is built on, so two rows
    that both matched would, by transitivity, also match each other and would
    already violate that constraint. There is nothing for it to break a tie
    among. The two functions still agree on which row wins: whatever name
    this function surfaces for a movement is the exact string
    resolve_movement() is later called with, and a query for an exact name
    can only ever match the row that owns it.
    """
    rows = await db.execute_fetchall(
        "SELECT name, section, metric FROM exercise_library WHERE archived = 0 ORDER BY display_order"
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
            # Nothing identifiable to surface — no movement name to put in
            # `unmatched`. Logged so it is at least visible in the container logs.
            logger.warning("WOD parser skipped a non-object entry: %r", item)
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
            # One malformed row must not lose the rest of the workout — but it
            # must not vanish either. `continue` alone put the movement in
            # neither `entries` nor `unmatched`, so the confirm screen showed no
            # trace of it: the user reviewed two of three movements, confirmed,
            # and the third had never existed.
            #
            # This needs no failure injection to hit. The system prompt above
            # invites exactly the shapes that break _coerce — "Rep schemes such
            # as 21-15-9" lands as `"reps": "21-15-9"`, and a weight comes back
            # as "43kg" often enough. Surfacing the movement as unmatched costs
            # its numbers but keeps the row on screen, where the user can map it.
            logger.warning("WOD parser could not coerce entry for %r: %r", canonical, item)
            if canonical not in unmatched:
                unmatched.append(canonical)
            continue
        entries.append(entry)

    return ParsedWod(entries=entries, unmatched=unmatched)
