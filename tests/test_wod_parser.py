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
    db = await aiosqlite.connect(tmp_path / "lib.db")
    db.row_factory = aiosqlite.Row
    await db.execute(
        """CREATE TABLE exercise_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            section TEXT NOT NULL,
            name TEXT NOT NULL,
            display_order INTEGER DEFAULT 0,
            metric TEXT NOT NULL DEFAULT 'reps',
            archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(category, name)
        )"""
    )
    return db


def test_warmup_section_movement_is_in_the_vocabulary(tmp_path):
    """The reported bug: a Warmup/Stretching row was invisible to the parser
    because canonical_movements() filtered category = 'CrossFit'. A session's
    warm-up and stretching are real movements the user already has in the
    library under other categories, and must now be recognised too."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (category, section, name, display_order, metric) "
                "VALUES ('Warmup', 'Warmup', 'Band Pull-apart', 1, 'reps')"
            )
            await db.commit()
            movements = await wod_parser.canonical_movements(db)
            assert "Band Pull-apart" in [m["name"] for m in movements]
        finally:
            await db.close()

    asyncio.run(run())


def test_duplicate_name_prefers_the_crossfit_row(tmp_path):
    """UNIQUE is (category, name), not (name) — 'Back Squat' exists under both
    Gym classics and CrossFit. The Gym classics row here is given the lower
    display_order AND a different section/metric than CrossFit's, so a wrong
    tie-break (first row, last row, lowest display_order regardless of
    category) would be caught by asserting on which section/metric wins."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (category, section, name, display_order, metric) "
                "VALUES ('Gym classics', 'Warmup', 'Back Squat', 1, 'time')"
            )
            await db.execute(
                "INSERT INTO exercise_library (category, section, name, display_order, metric) "
                "VALUES ('CrossFit', 'Core', 'Back Squat', 50, 'reps')"
            )
            await db.commit()
            movements = await wod_parser.canonical_movements(db)
            matches = [m for m in movements if m["name"] == "Back Squat"]
            assert len(matches) == 1, "a duplicate name must appear exactly once, not twice"
            assert matches[0]["section"] == "Core", "the CrossFit row's section must win"
            assert matches[0]["metric"] == "reps", "the CrossFit row's metric must win"
        finally:
            await db.close()

    asyncio.run(run())


def test_duplicate_name_without_crossfit_prefers_lowest_display_order(tmp_path):
    """When neither duplicate is CrossFit, the tie-break falls back to lowest
    display_order — asserted here via section/metric so a reversed comparison
    (highest wins) or "last row wins" would fail this."""

    async def run():
        db = await _real_library_db(tmp_path)
        try:
            await db.execute(
                "INSERT INTO exercise_library (category, section, name, display_order, metric) "
                "VALUES ('Cardio', 'Cardio', 'Row', 1, 'time')"
            )
            await db.execute(
                "INSERT INTO exercise_library (category, section, name, display_order, metric) "
                "VALUES ('Gym classics', 'Core', 'Row', 30, 'reps')"
            )
            await db.commit()
            movements = await wod_parser.canonical_movements(db)
            matches = [m for m in movements if m["name"] == "Row"]
            assert len(matches) == 1
            assert matches[0]["metric"] == "time", "the lower display_order (Cardio) row must win"
        finally:
            await db.close()

    asyncio.run(run())
