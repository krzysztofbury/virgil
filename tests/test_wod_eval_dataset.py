"""The WOD eval's golden cases (#155) agree with the vocabulary the parser is given."""

import asyncio
import re
from collections import Counter

import aiosqlite
import pytest

from evals.wod.run import load_cases

CASES = load_cases()
FIELDS = {"movement", "set_number", "reps", "weight", "duration", "equipment"}


@pytest.fixture(scope="module")
def vocabulary(tmp_path_factory):
    async def build():
        from app.migrations.runner import run_migrations
        from app.services.wod_parser import canonical_movements

        db = await aiosqlite.connect(tmp_path_factory.mktemp("vocab") / "u.db")
        db.row_factory = aiosqlite.Row
        await run_migrations(db)
        movements = await canonical_movements(db)
        await db.close()
        return movements

    movements = asyncio.run(build())
    names = {m["name"] for m in movements}
    spellings = {s.lower() for m in movements for s in [m["name"], *m["aliases"]]}
    return names, spellings


def test_cases_are_well_formed_and_unique():
    assert len(CASES) >= 30
    assert len({case["id"] for case in CASES}) == len(CASES)
    for case in CASES:
        assert case["note"].strip() and case["tags"], case["id"]
        for entry in case["expected"]["entries"]:
            assert set(entry) == FIELDS, case["id"]


def test_expected_movements_are_canonical_names(vocabulary):
    names, _spellings = vocabulary
    unknown = {e["movement"] for case in CASES for e in case["expected"]["entries"]} - names
    assert unknown == set()


def test_expected_unmatched_names_are_outside_the_vocabulary(vocabulary):
    _names, spellings = vocabulary
    inside = [
        (case["id"], name) for case in CASES for name in case["expected"]["unmatched"] if name.lower() in spellings
    ]
    assert inside == []


def test_sets_number_from_one_without_gaps():
    for case in CASES:
        by_movement: dict[str, list[int]] = {}
        for entry in case["expected"]["entries"]:
            by_movement.setdefault(entry["movement"], []).append(entry["set_number"])
        for movement, numbers in by_movement.items():
            assert numbers == list(range(1, len(numbers) + 1)), (case["id"], movement)


def test_the_cases_cover_what_the_parser_meets():
    tags = Counter(tag for case in CASES for tag in case["tags"])
    for tag, least in {
        "polish": 4,
        "mixed": 2,
        "oov": 5,
        "alias": 4,
        "equipment": 3,
        "missing": 3,
        "duration": 4,
    }.items():
        assert tags[tag] >= least, tag
    assert any(len(case["expected"]["entries"]) >= 30 for case in CASES), "one note near the truncation risk"


EQUIPMENT_WORDS = {
    r"\bbarbell\b": "barbell",
    r"\bsztang": "barbell",
    r"\bdumbbell\b": "dumbbell",
    r"\bdb\b": "dumbbell",
    r"\bkettlebell\b": "kettlebell",
    r"\bkb\b": "kettlebell",
    r"\bballs?\b": "ball",
}


def test_an_equipment_word_in_the_note_sets_equipment_on_an_entry():
    """One rule for every case: the prompt's "only when the note says it", read
    literally, also when the word is part of the movement's name (KB swing,
    wall balls, barbell row)."""
    missing = []
    for case in CASES:
        given = {entry["equipment"] for entry in case["expected"]["entries"]}
        for pattern, equipment in EQUIPMENT_WORDS.items():
            if re.search(pattern, case["note"], re.IGNORECASE) and equipment not in given:
                missing.append((case["id"], equipment))
    assert missing == []
