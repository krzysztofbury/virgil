"""Parse a free-text WOD note into structured training entries.

The user writes one messy line from memory after training; an LLM turns it into
entries. The model is constrained to the movement vocabulary held in
exercise_library — every non-archived row, not just the ones labelled
'CrossFit' — without a closed list it invents 'Thruster', 'thrusters' and
'Thruster 43kg' as three movements and the catalogue rots within a month.

Anything the model returns outside that vocabulary is reported as unmatched
rather than created, so the user resolves it on the confirmation screen. So is
an entry for a listed movement the note never writes by its name or an alias,
and equipment survives only where the note states it (#187): the first live
evaluation found smaller models doing both, against the prompt.
"""

import logging
import re
from dataclasses import dataclass, field

from app.services.llm import call_llm, parse_andy_response
from app.services.movement_vocab import EQUIPMENT

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

# The confirm screen refuses a submission above this row count, and that refusal
# costs the user the whole form (app.routers.training.MAX_CONFIRM_ENTRIES, which
# a test pins to this value). So the parser truncates here instead: 200 rows the
# user can save beat 250 rows the user can only discard.
MAX_PARSED_ENTRIES = 200

_SYSTEM_PROMPT = """You extract structured training data from a short, messy \
note a CrossFit athlete wrote from memory after a session. The note may be in \
Polish or English and may mix both.

Return ONLY a JSON object of this exact shape:
{"entries": [{"movement": str, "set_number": int, "reps": int|null,
 "weight": float|null, "duration": float|null, "equipment": str|null, "note": str}], "unmatched": [str]}

Rules:
- "movement" MUST be copied verbatim from the allowed movement list below. \
Never invent a movement name and never translate one.
- A list line may show spellings after "also written:". A note using one of \
them means that movement; still return the list name, never the spelling.
- Any movement in the note that is not in the list goes into "unmatched" as the \
user wrote it. Do not guess a near match.
- "equipment" is one of: barbell, dumbbell, kettlebell, plate, bodyweight, ball, \
band, machine, other - only when the note says it (DB = dumbbell, KB = \
kettlebell); otherwise null. "dumbbell hang snatch" is Hang Snatch with \
equipment dumbbell.
- Rep schemes such as "21-15-9" produce one entry per round, set_number 1,2,3, \
with reps 21, 15, 9 and the scheme copied into "note".
- "5x5 @70" produces 5 entries, set_number 1..5, reps 5, weight 70.
- "weight" is kilograms. "duration" is seconds.
- A metcon result (a finishing time, or rounds+reps for an AMRAP) goes into the \
"note" of that movement's first entry. Do not invent an entry for it.
- Omit nothing you can map; guess nothing you cannot.

Allowed movements:
"""


# The words a note uses for each equipment value: the prompt's own (DB, KB) and
# their Polish forms. A model's equipment survives only when its word is in the
# note (#187): the smaller models fill it in from the movement ("back squat" ->
# barbell) although the prompt says not to. "other" has no word, so it never
# survives.
EQUIPMENT_WORDS = (
    (re.compile(r"\b(?:barbells?|sztang\w*)\b", re.IGNORECASE), "barbell"),
    (re.compile(r"\b(?:dumbbells?|db|hantl\w*)\b", re.IGNORECASE), "dumbbell"),
    (re.compile(r"\b(?:kettlebells?|kb|odważnik\w*)\b", re.IGNORECASE), "kettlebell"),
    (re.compile(r"\b(?:plates?|talerz\w*)\b", re.IGNORECASE), "plate"),
    (re.compile(r"\b(?:bodyweight|body weight)\b", re.IGNORECASE), "bodyweight"),
    (re.compile(r"\b(?:balls?|piłk\w*)\b", re.IGNORECASE), "ball"),
    (re.compile(r"\b(?:bands?|gum\w*)\b", re.IGNORECASE), "band"),
    (re.compile(r"\b(?:machines?|maszyn\w*)\b", re.IGNORECASE), "machine"),
)

# A word, with an apostrophe inside it kept ("farmer's").
_WORD = re.compile(r"\w+(?:['’]\w+)*")


def _words(text: str) -> list[tuple[str, int, int]]:
    return [(m.group().lower().replace("'", "").replace("’", ""), m.start(), m.end()) for m in _WORD.finditer(text)]


# English endings and the Polish ones a gym word takes ("pull-upy", "OHS-ów").
_SHORT_ENDINGS = ("", "s", "es", "ing", "ed", "y", "i", "a", "e", "u", "ów", "om", "em", "ach", "ami")


def _same_word(written: str, listed: str) -> bool:
    """A written word for a listed one, whatever its ending.

    A listed word of four letters or more may take any ending (a plural, a
    Polish case ending). A shorter one takes only a listed ending, so "DU" does
    not match "dumbbell" and "Run" does not match "rundy".
    """
    if len(listed) >= 4:
        return written.startswith(listed)
    return any(written == listed + ending for ending in _SHORT_ENDINGS)


def _occurrences(term: str, note_words: list[tuple[str, int, int]]) -> list[tuple[int, int]]:
    """Where the note writes `term`, as character spans.

    The term's words must appear in order, each as the same word (_same_word),
    or run together ("pullups" for Pull-up). A misspelling ("pul ups") or another
    word ("bieg" for Run) does not match.
    """
    parts = [word for word, _, _ in _words(term)]
    if not parts:
        return []
    joined = "".join(parts)
    spans = []
    for i, (word, start, end) in enumerate(note_words):
        if len(parts) > 1 and _same_word(word, joined):
            spans.append((start, end))
        window = note_words[i : i + len(parts)]
        if len(window) == len(parts) and all(_same_word(w, p) for (w, _, _), p in zip(window, parts, strict=True)):
            spans.append((start, window[-1][2]))
    return spans


# A note lists its parts on lines or between commas and semicolons.
_PART = re.compile(r"[^,;\n]+")


def _unwritten_parts(text: str, written: list[tuple[int, int]], reported: list[str]) -> list[str]:
    """The parts of the note that write no listed movement and that the model
    did not report itself: where a refused entry's movement was written."""
    reported_lower = [name.lower() for name in reported]
    parts = []
    for m in _PART.finditer(text):
        part = m.group().strip(" \t:.-")
        if not _WORD.search(part) or any(m.start() < end and start < m.end() for start, end in written):
            continue
        if any(name in part.lower() or part.lower() in name for name in reported_lower):
            continue
        parts.append(part)
    return parts


def _equipment_in(text: str, offset: int = 0) -> list[tuple[int, str]]:
    return sorted((m.start() + offset, value) for pattern, value in EQUIPMENT_WORDS for m in pattern.finditer(text))


@dataclass(frozen=True)
class ParsedEntry:
    movement: str
    set_number: int
    reps: int | None
    weight: float | None
    duration: float | None
    note: str
    equipment: str | None = None


@dataclass(frozen=True)
class ParsedWod:
    entries: list[ParsedEntry] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    # Rows cut to stay inside MAX_PARSED_ENTRIES. The confirm screen states the
    # number: a silent truncation reads as "this is everything you did".
    dropped: int = 0


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

    aliases = await db.execute_fetchall(
        "SELECT a.alias, l.name FROM exercise_aliases a JOIN exercise_library l ON l.id = a.library_id "
        "WHERE l.archived = 0 ORDER BY a.alias"
    )
    by_name: dict[str, list[str]] = {}
    for alias, name in aliases:
        by_name.setdefault(name.lower(), []).append(alias)
    for movement in movements:
        movement["aliases"] = by_name.get(movement["name"].lower(), [])

    assert len(movements) <= MAX_LIBRARY_MOVEMENTS, (
        f"movement vocabulary has grown to {len(movements)} rows "
        f"(max {MAX_LIBRARY_MOVEMENTS}) — prune exercise_library before the WOD "
        "parser's system prompt grows any further"
    )
    return movements


def _entry_equipment(model_value, spans: list[tuple[int, int]], text: str, free: set[str]) -> str | None:
    """The equipment the note states for this entry.

    The word in the movement as written wins ("KB swings" is kettlebell whatever
    the model said, and "Barbell Row" is barbell when the model forgot it). Else
    the model's value stands only when a free equipment word in the note says it.
    """
    for start, end in spans:
        named = _equipment_in(text[start:end], start)
        if named:
            return named[0][1]
    return model_value if model_value in EQUIPMENT and model_value in free else None


def _coerce(value, caster):
    if value is None:
        return None
    try:
        return caster(value)
    except TypeError, ValueError:
        raise


async def parse_wod(db, text: str) -> ParsedWod:
    """Turn a free-text WOD note into entries constrained to the library."""
    if not text or not text.strip():
        raise ValueError("WOD text is empty")

    text = text.strip()[:MAX_WOD_CHARS]
    movements = await canonical_movements(db)
    by_lower = {alias.lower(): m["name"] for m in movements for alias in m["aliases"]}
    by_lower.update({m["name"].lower(): m["name"] for m in movements})

    system_prompt = _SYSTEM_PROMPT + "\n".join(
        f"- {m['name']}" + (f" (also written: {', '.join(m['aliases'])})" if m["aliases"] else "") for m in movements
    )
    # Bound thinking and budget generously - this call is the token-hungriest in
    # the app and it was the only structured one still asking for neither.
    #
    # The reported failure: a note reading "amrap 20 minutes, 7 series, 5x pull
    # up, 10x push ups, 15 squats" plus a warm-up and 6 snatch singles. The
    # one-entry-per-round rule above makes that ~28 entries, roughly 1200 output
    # tokens of JSON - an order of magnitude more than any other caller here
    # produces. Against gemini/gemini-3-pro-preview the response came back cut
    # off after 837 characters, mid-object, and 25 correctly-parsed movements
    # were thrown away with it.
    #
    # call_llm always sends an effort now - the user's Settings choice, default
    # medium - so litellm never omits thinkingConfig and Gemini 3 never falls
    # back to its own default level (high on Pro), which is what consumed nearly
    # the whole 4096 allowance. The old hardcoded "disable" is gone because
    # OpenAI rejects that value with a 400.
    #
    # The effort is a request for a level, never a guarantee of none: Gemini 3
    # Pro cannot turn thinking off, and litellm clamps the cheapest values to
    # thinkingLevel 'low'. That is exactly why the cap below has to be generous
    # rather than merely sufficient, and why raising the setting is safe here.
    #
    # MAX_WOD_CHARS (4000) bounds the input, so the output cannot grow without
    # bound either; 16384 clears the worst realistic note with thinking headroom
    # to spare. A cap costs nothing when unused - only generated tokens are
    # billed - whereas one set too low costs the user their whole session.
    raw = await call_llm(db, system_prompt, text, json_mode=True, max_tokens=16384)
    payload = parse_andy_response(raw)

    raw_entries = payload.get("entries")
    raw_unmatched = payload.get("unmatched")
    entries: list[ParsedEntry] = []
    unmatched: list[str] = [str(u) for u in raw_unmatched] if isinstance(raw_unmatched, list) else []

    # Where the note writes each movement, by its name or any alias. An entry
    # for a movement the note never writes is refused (#187): the smaller models
    # map an unlisted name ("bieg", "pul ups") onto a listed movement, which
    # would hide exactly the gap `unmatched` exists to show.
    note_words = _words(text)
    written: dict[str, list[tuple[int, int]]] = {}
    for m in movements:
        spans = [span for term in [m["name"], *m["aliases"]] for span in _occurrences(term, note_words)]
        if spans:
            written[m["name"]] = spans
    items = raw_entries if isinstance(raw_entries, list) else []
    returned = {
        by_lower.get(str(item.get("movement") or "").strip().lower()) for item in items if isinstance(item, dict)
    }
    # An equipment word inside a returned movement as written ("KB swings",
    # "wall balls") belongs to that movement; the note's other ones are free.
    claimed = [span for name in returned if name in written for span in written[name]]
    free_equipment = {
        value for at, value in _equipment_in(text) if not any(start <= at < end for start, end in claimed)
    }
    # A refused entry is reported by what the note wrote for it, in order, while
    # an unclaimed part of the note is left; after that, by the movement's name.
    unwritten = _unwritten_parts(text, [span for spans in written.values() for span in spans], unmatched)

    refused: set[str] = set()
    for item in items:
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
        if canonical not in written:
            if canonical not in refused:
                refused.add(canonical)
                logger.warning("WOD parser refused %r: the note writes no listed name or alias for it", canonical)
                label = unwritten.pop(0) if unwritten else canonical
                if label not in unmatched:
                    unmatched.append(label)
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
                equipment=_entry_equipment(item.get("equipment"), written[canonical], text, free_equipment),
            )
        except TypeError, ValueError:
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

    # Both lists occupy confirm rows, so both are bounded. unmatched is cut
    # first: an unmatched name carries no numbers, so it is the cheaper row to
    # lose. Bounding entries alone left the total unbounded, which is the same
    # unsavable form with a different cause.
    dropped = 0
    if len(unmatched) > MAX_PARSED_ENTRIES:
        dropped += len(unmatched) - MAX_PARSED_ENTRIES
        unmatched = unmatched[:MAX_PARSED_ENTRIES]
    room = MAX_PARSED_ENTRIES - len(unmatched)
    if len(entries) > room:
        dropped += len(entries) - room
        entries = entries[:room]
    if dropped:
        logger.warning("WOD parser truncated %s rows past the confirm limit of %s", dropped, MAX_PARSED_ENTRIES)

    assert len(entries) + len(unmatched) <= MAX_PARSED_ENTRIES, (
        f"parser returned {len(entries) + len(unmatched)} confirm rows, above {MAX_PARSED_ENTRIES}"
    )
    return ParsedWod(entries=entries, unmatched=unmatched, dropped=dropped)
