"""The confirm screen renders one row contract and one grouped movement picker.

Four copies of the same row markup used to exist (parsed, unmatched, seed and
the Alpine template), each with its own index arithmetic and its own flat
option list holding the whole exercise library.
"""

import inspect
import json
import sqlite3
from pathlib import Path

from conftest import user_db_path

from app.routers.training import _confirm_rows


def _new_parsed_session(parsed):
    conn = sqlite3.connect(user_db_path())
    try:
        cur = conn.execute(
            "INSERT INTO training_sessions (date, notes, wod_parsed) VALUES ('2026-08-26', 'picker probe', ?)",
            (json.dumps(parsed),),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_confirm_rows_numbers_every_row_type_once():
    parsed = {
        "entries": [
            {
                "movement": "Thruster",
                "set_number": 1,
                "reps": 21,
                "weight": 43.0,
                "duration": None,
                "note": "21-15-9",
            }
        ],
        "unmatched": ["burpee box over"],
        "parse_error": "",
    }
    rows = _confirm_rows(parsed, seed_rows=5)
    assert [r["index"] for r in rows] == [0, 1]
    assert rows[0]["movement"] == "Thruster"
    assert rows[0]["reps"] == "21"
    assert rows[0]["duration"] == "", "a missing value must render blank, never the string None"
    assert rows[1]["movement"] == ""
    assert rows[1]["unmatched_label"] == "burpee box over"


def test_confirm_rows_seeds_when_nothing_parsed():
    rows = _confirm_rows({"entries": [], "unmatched": [], "parse_error": "boom"}, seed_rows=5)
    assert [r["index"] for r in rows] == [0, 1, 2, 3, 4]
    assert all(r["movement"] == "" and r["set_number"] == "1" for r in rows)


def test_confirm_rows_seeds_nothing_without_a_vocabulary():
    """No library means no picker, so a seed row could not write anything."""
    rows = _confirm_rows({"entries": [], "unmatched": [], "parse_error": ""}, seed_rows=0)
    assert rows == []


def test_picker_groups_by_section_and_carries_tags(auth_client):
    session_id = _new_parsed_session({"entries": [], "unmatched": ["nieznany ruch"], "parse_error": ""})
    html = auth_client.get(f"/training/wod/confirm/{session_id}").text
    assert "<optgroup" in html, "the picker is still a flat select"
    assert "data-tags=" in html, "options carry no tags to search on"
    assert "data-recent=" in html, "recently used movements are not marked"
    assert 'type="search"' in html, "the picker has no search input"
    assert "nieznany ruch" in html, "the unmatched name must stay visible while it is resolved"


def test_every_row_uses_the_shared_macro():
    """One picker contract for every row: the option list lives in the partial."""
    template = Path("app/templates/wod_confirm.html").read_text(encoding="utf-8")
    assert "<option value=" not in template, (
        "wod_confirm.html still builds its own option list; every row must call the macro"
    )
    assert 'import "partials/wod_row.html"' in template or "partials/wod_row.html" in template


def test_parser_vocabulary_keeps_its_shape():
    """canonical_movements stays a parser function: name, section, metric.

    Tags are a picker concern. Joining the tag table into the prompt vocabulary
    put a picker query on every parse and broke the fixture that builds
    exercise_library without the tag table.
    """
    from app.services import wod_parser

    source = inspect.getsource(wod_parser.canonical_movements)
    assert "exercise_library_tags" not in source
