"""WOD free-text → structured entries. The LLM is always stubbed."""

import asyncio
import json

import aiosqlite
import pytest

from app.services import wod_parser


class _FakeDB:
    """Minimal db double: only execute_fetchall is used by the parser."""

    def __init__(self, rows):
        self._rows = rows

    async def execute_fetchall(self, *_args, **_kwargs):
        return self._rows


_LIBRARY = [
    {"name": "Back Squat", "section": "Core", "metric": "reps"},
    {"name": "Thruster", "section": "Core", "metric": "reps"},
    {"name": "Pull-up", "section": "Core", "metric": "reps"},
    {"name": "Row", "section": "Cardio", "metric": "time"},
]


def _stub_llm(monkeypatch, payload: dict):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return json.dumps(payload)

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)


def test_parses_strength_and_metcon(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Back Squat", "set_number": 1, "reps": 5, "weight": 70.0, "duration": None, "note": ""},
                {
                    "movement": "Thruster",
                    "set_number": 1,
                    "reps": 21,
                    "weight": 43.0,
                    "duration": None,
                    "note": "21-15-9",
                },
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "back squat 5x5 70, potem 21-15-9 thruster 43"))
    assert [e.movement for e in result.entries] == ["Back Squat", "Thruster"]
    assert result.entries[0].reps == 5
    assert result.entries[0].weight == 70.0
    assert result.entries[1].note == "21-15-9"
    assert result.unmatched == []


def test_movement_outside_vocabulary_goes_to_unmatched(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Devil Press", "set_number": 1, "reps": 10, "weight": 22.5, "duration": None, "note": ""},
                {"movement": "Pull-up", "set_number": 1, "reps": 10, "weight": None, "duration": None, "note": ""},
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "devil press 10, pullups 10"))
    assert [e.movement for e in result.entries] == ["Pull-up"]
    assert result.unmatched == ["Devil Press"]


def test_movement_match_is_case_insensitive(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "back squat", "set_number": 1, "reps": 5, "weight": 70.0, "duration": None, "note": ""}
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "back squat 5x5 70"))
    assert result.entries[0].movement == "Back Squat", "must normalise to the canonical spelling"


def test_llm_unmatched_list_is_preserved(monkeypatch):
    _stub_llm(monkeypatch, {"entries": [], "unmatched": ["jakiś dziwny ruch"]})
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "jakiś dziwny ruch"))
    assert result.unmatched == ["jakiś dziwny ruch"]


def test_malformed_entry_is_skipped_not_crashing(monkeypatch):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Back Squat", "set_number": "not-a-number", "reps": 5, "weight": 70.0},
                {"movement": "Pull-up", "set_number": 1, "reps": 10, "weight": None, "duration": None, "note": ""},
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "..."))
    assert [e.movement for e in result.entries] == ["Pull-up"]


def test_empty_text_raises(monkeypatch):
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "   "))


def test_prompt_carries_the_closed_vocabulary(monkeypatch):
    seen = {}

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        seen["system"] = system_prompt
        return json.dumps({"entries": [], "unmatched": []})

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "cokolwiek"))
    assert "Back Squat" in seen["system"]
    assert "Row" in seen["system"]


def test_set_number_zero_is_clamped_with_warning(monkeypatch, caplog):
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {
                    "movement": "Back Squat",
                    "set_number": 0,
                    "reps": 5,
                    "weight": 70.0,
                    "duration": None,
                    "note": "",
                }
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "back squat"))
    assert len(result.entries) == 1
    assert result.entries[0].set_number == 1
    assert "clamped set_number" in caplog.text


def test_container_type_guard_prevents_string_iteration(monkeypatch):
    _stub_llm(
        monkeypatch,
        {"entries": "nonsense", "unmatched": "abc"},
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "anything"))
    assert result.entries == []
    assert result.unmatched == []


def test_vocabulary_bound_is_enforced(monkeypatch):
    """I5 reproduction: canonical_movements() had no LIMIT and no count check,
    and parse_wod concatenates every row into the system prompt unchecked.
    POST /api/library is agent-callable — an agent looping on add_exercise is
    exactly what an MCP-writable dictionary invites — and the cost of an
    unbounded vocabulary is paid on every subsequent parse until the prompt
    breaks the model's context window. This must now fail loudly instead of
    silently growing the prompt forever.
    """
    oversized_library = [
        {"name": f"Movement {i}", "section": "Core", "metric": "reps"}
        for i in range(wod_parser.MAX_LIBRARY_MOVEMENTS + 1)
    ]

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        # Must never be reached: canonical_movements() should raise before
        # parse_wod gets this far. Deliberately worded without "vocabulary" or
        # a row count, so this can't accidentally satisfy the match= below for
        # the wrong reason if the real bound check is removed.
        raise AssertionError("the LLM stub fired — the precondition check should have failed first")

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    with pytest.raises(AssertionError, match=r"has grown to 501 rows \(max 500\)"):
        asyncio.run(wod_parser.parse_wod(_FakeDB(oversized_library), "anything"))


def test_vocabulary_at_the_bound_is_accepted(monkeypatch):
    """The bound must not be off-by-one in the wrong direction: exactly
    MAX_LIBRARY_MOVEMENTS rows must still parse normally."""
    library_at_bound = [
        {"name": f"Movement {i}", "section": "Core", "metric": "reps"} for i in range(wod_parser.MAX_LIBRARY_MOVEMENTS)
    ]
    _stub_llm(monkeypatch, {"entries": [], "unmatched": []})
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(library_at_bound), "anything"))
    assert result.entries == []
    assert result.unmatched == []


# ── canonical_movements() against a real exercise_library table ─────────────
# _FakeDB above ignores the SQL entirely and hands back a canned row list, so
# it cannot exercise the real WHERE/ORDER BY that does the whole-library scope
# and the dedupe tie-break — these tests run the query against real SQLite.


async def _real_library_db(tmp_path):
    """Post-019 shape: no `category`, UNIQUE(name COLLATE NOCASE) — migration
    019 rebuilds exercise_library into this shape on every install, fresh or
    upgraded (009 still seeds the OLD category-bearing shape; 019 is the one
    and only conversion path — see its module docstring)."""
    db = await aiosqlite.connect(tmp_path / "lib.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(name COLLATE NOCASE)
        )"""
    )
    return db


def test_warmup_section_movement_is_in_the_vocabulary(tmp_path):
    """The reported bug: a Warmup/Stretching row was invisible to the parser
    because canonical_movements() filtered category = 'CrossFit'. A session's
    warm-up and stretching are real movements the user already has in the
    library under other tags (or none), and must now be recognised too."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Warmup', 'Band Pull-apart', 1, 'reps')"
            )
            await db.commit()
            movements = await wod_parser.canonical_movements(db)
            assert "Band Pull-apart" in [m["name"] for m in movements]
        finally:
            await db.close()

    asyncio.run(run())


def test_duplicate_library_name_is_rejected_by_unique_constraint(tmp_path):
    """Before migration 019, UNIQUE was (category, name), so 'Back Squat' could
    exist under both Gym classics and CrossFit — canonical_movements() then
    needed an explicit CrossFit-preferring tie-break to avoid silently
    mis-typing the duplicate. UNIQUE is now (name COLLATE NOCASE), so that
    scenario is no longer a tie-break case to get right; it is a write the
    database itself refuses. The second insert deliberately differs only by
    case ('back squat' vs 'Back Squat') — a binary UNIQUE(name), with no
    COLLATE, would let this one through and this test would pass for the
    wrong reason."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Warmup', 'Back Squat', 1, 'time')"
            )
            await db.commit()
            try:
                await db.execute(
                    "INSERT INTO exercise_library (section, name, display_order, metric) "
                    "VALUES ('Core', 'back squat', 50, 'reps')"
                )
                raised = False
            except aiosqlite.IntegrityError:
                raised = True
            assert raised, "a case-variant duplicate name must be rejected by UNIQUE(name COLLATE NOCASE)"
            movements = await wod_parser.canonical_movements(db)
            matches = [m for m in movements if m["name"] == "Back Squat"]
            assert len(matches) == 1, "only the original row must exist"
            assert matches[0]["section"] == "Warmup"
            assert matches[0]["metric"] == "time"
        finally:
            await db.close()

    asyncio.run(run())


def test_seen_dedupe_collapses_non_ascii_case_duplicates(tmp_path):
    """The `seen` dedupe loop is NOT dead code: SQLite's UNIQUE(name COLLATE
    NOCASE) only folds ASCII case, so 'Ćwiczenie' and 'ćwiczenie' — differing
    only in a non-ASCII letter's case — both satisfy the constraint as
    distinct rows (proven below: the second INSERT does not raise). A
    hand-edited or otherwise malformed database (something writing directly
    to the table, bypassing validate_library_write's Unicode-aware dup check)
    can therefore still produce this pair. Without the Python-level `seen`
    dedupe, canonical_movements() would hand the LLM both spellings of the
    same movement in its system prompt. This test would fail if that loop
    were deleted: the raw SELECT has no WHERE on name, so both rows always
    come back from SQLite; only the dedupe loop collapses them to one."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Core', 'Ćwiczenie', 1, 'reps')"
            )
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Cardio', 'ćwiczenie', 2, 'time')"
            )
            await db.commit()

            # Confirm the premise: SQLite's NOCASE really does let this pair
            # coexist -- if a future SQLite version closed this gap, this
            # assertion (not the one below) is what should fail.
            rows = await db.execute_fetchall("SELECT name FROM exercise_library")
            assert len(rows) == 2, "both non-ASCII case-variant rows must have been accepted by the UNIQUE constraint"

            movements = await wod_parser.canonical_movements(db)
            matches = [m for m in movements if m["name"].lower() == "ćwiczenie"]
            assert len(matches) == 1, "the two Unicode-only-differing rows must collapse to one vocabulary entry"
            assert matches[0]["name"] == "Ćwiczenie", "first-by-display_order (id 1) must be the survivor"
            assert matches[0]["section"] == "Core"
            assert matches[0]["metric"] == "reps"
        finally:
            await db.close()

    asyncio.run(run())


def test_canonical_movements_orders_by_display_order(tmp_path):
    """The dedupe-by-first-seen-name loop in canonical_movements() only does
    something observable if the query itself returns rows in display_order —
    this pins that ordering directly, independent of the (now impossible)
    duplicate-name scenario the old CrossFit tie-break test covered.

    Back Squat (display_order 30) is inserted BEFORE Row (display_order 1) on
    purpose: SQLite's default rowid order would then put Back Squat first,
    the opposite of what display_order demands, so an accidentally-deleted
    `ORDER BY display_order` fails this instead of coincidentally passing."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Core', 'Back Squat', 30, 'reps')"
            )
            await db.execute(
                "INSERT INTO exercise_library (section, name, display_order, metric) "
                "VALUES ('Cardio', 'Row', 1, 'time')"
            )
            await db.commit()
            movements = await wod_parser.canonical_movements(db)
            names = [m["name"] for m in movements]
            assert names == ["Row", "Back Squat"], "movements must come back in display_order"
        finally:
            await db.close()

    asyncio.run(run())


def test_uncoercible_entry_is_surfaced_as_unmatched_not_dropped(monkeypatch):
    """A row the coercion cannot handle must stay visible on the confirm screen.

    It used to go into neither `entries` nor `unmatched`, so the movement simply
    vanished: the user reviewed the rows that survived, confirmed, and the rest
    had never existed. No failure injection is needed to reach this — the system
    prompt invites "21-15-9" rep schemes, which arrive as a string in `reps`.
    """
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": "21-15-9", "weight": 43.0},
                {"movement": "Back Squat", "set_number": 1, "reps": 5, "weight": 70.0},
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "21-15-9 thruster 43, back squat 5x5 70"))
    names = [e.movement for e in result.entries]
    assert "Back Squat" in names, "the well-formed row must still be parsed"
    assert "Thruster" not in names, "the uncoercible row cannot become an entry"
    assert "Thruster" in result.unmatched, (
        "an uncoercible row must be surfaced for the user to map, not silently dropped"
    )


def test_parse_bounds_thinking_and_budgets_the_output(monkeypatch):
    """The reported bug's root cause.

    parse_wod was the only structured caller passing neither reasoning_effort nor
    a budget sized for its own output. Against gemini/gemini-3-pro-preview
    thinking then ran unbounded inside a 4096-token cap, and a note that expands
    to ~28 entries (an AMRAP's rounds, one entry each) came back truncated after
    837 characters - 25 correctly-parsed movements discarded with it.

    Pinned as an explicit contract because the failure is invisible locally: the
    LLM is stubbed in every other test here, so a budget regression would show up
    only in production, only on long notes, and only on a thinking model.
    """
    seen = {}

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        seen.update(kwargs)
        return json.dumps({"entries": [], "unmatched": []})

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "cindy"))

    assert seen.get("json_mode") is True
    assert seen.get("reasoning_effort") == "disable", "thinking must be capped, not left to eat the output budget"
    assert seen.get("max_tokens", 0) >= 16384, (
        "the budget must clear a full AMRAP expansion plus whatever thinking litellm's drop_params lets through"
    )


def test_omitting_reasoning_effort_really_does_leave_thinking_at_the_default():
    """Confirm the premise the budget fix rests on, at the litellm boundary.

    Two facts, neither obvious from the call site, and both load-bearing:

      1. Omitting reasoning_effort sends NO thinkingConfig, so Gemini 3 picks its
         own default level - it does not mean "no thinking". That is what ate the
         old 4096-token allowance.
      2. "disable" is clamped to thinkingLevel 'low', not dropped and not
         honoured as off, because Gemini 3 Pro cannot disable thinking. So the
         parser still pays for thinking and the max_tokens cap must absorb it.

    If a litellm upgrade changes either - starts dropping the parameter, or gains
    a genuine off - this assertion is what should fail, so the max_tokens choice
    can be revisited deliberately rather than silently.
    """
    from litellm.utils import get_optional_params

    def thinking_for(**kwargs):
        params = get_optional_params(
            model="gemini-3-pro-preview",
            custom_llm_provider="gemini",
            max_tokens=16384,
            response_format={"type": "json_object"},
            drop_params=True,
            **kwargs,
        )
        return params.get("thinkingConfig")

    assert thinking_for() is None, "no reasoning_effort means no thinkingConfig, so the model's own default applies"
    assert thinking_for(reasoning_effort="disable") == {"thinkingLevel": "low", "includeThoughts": False}, (
        "'disable' is the cheapest level this model family offers, not off - the token cap must still cover thinking"
    )


def test_truncated_response_still_yields_the_movements_that_arrived(monkeypatch):
    """End-to-end recovery: parse_wod must survive the exact prod failure.

    The response below is cut mid-object inside `entries`, three levels deep -
    the shape parse_andy_response's old one-level repair could never close. The
    whole session used to be lost; the movements that did arrive must now come
    through.
    """
    full = {
        "entries": [
            {"movement": "Row", "set_number": 1, "reps": None, "weight": None, "duration": 180.0, "note": "warmup"},
            {"movement": "Pull-up", "set_number": 1, "reps": 5, "weight": None, "duration": None, "note": "cindy"},
            {"movement": "Pull-up", "set_number": 2, "reps": 5, "weight": None, "duration": None, "note": ""},
        ],
        "unmatched": [],
    }
    text = json.dumps(full, indent=2)
    truncated = text[: text.rindex('"set_number": 2') + len('"set_number": 2')]

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return truncated

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "..."))

    assert [e.movement for e in result.entries] == ["Row", "Pull-up", "Pull-up"]
    assert result.entries[0].duration == 180.0
    assert result.entries[0].note == "warmup"
    assert result.entries[1].reps == 5
    assert result.entries[2].set_number == 2, "the half-written entry keeps the fields that did arrive"
    assert result.entries[2].reps is None, "and honestly reports the ones that did not"


def test_uncoercible_entry_is_not_duplicated_in_unmatched(monkeypatch):
    """Two bad rows for one movement must not stack up two identical rows."""
    _stub_llm(
        monkeypatch,
        {
            "entries": [
                {"movement": "Thruster", "set_number": 1, "reps": "21-15-9"},
                {"movement": "Thruster", "set_number": 2, "reps": "15-9"},
            ],
            "unmatched": [],
        },
    )
    result = asyncio.run(wod_parser.parse_wod(_FakeDB(_LIBRARY), "21-15-9 thruster"))
    assert result.unmatched.count("Thruster") == 1
