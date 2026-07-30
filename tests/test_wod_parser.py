"""WOD free-text → structured entries. The LLM is always stubbed."""

import asyncio
import json

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
