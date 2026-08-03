"""Truncated-JSON salvage in parse_andy_response.

The reported bug: a CrossFit note ("Cindy" - an AMRAP whose 7 rounds legitimately
expand to 21 entries) came back from gemini/gemini-3-pro-preview truncated at
837 characters, cut mid-object inside the "entries" array:

    {"entries": [ ...25 complete entries... ,
      {"movement": "Pull-up",
       "set_number": 5            <- output stopped here

parse_andy_response's repair layer only ever tried appending "}" or '"}', which
can close exactly ONE level. The real truncation sits three deep (outer object →
array → entry object), so the repair could never fire for the WOD parser - it
raised, and a whole session's worth of correctly-parsed movements was discarded.
"""

import json

import pytest

from app.services.llm import parse_andy_response


def _cindy_payload() -> dict:
    """The entries the reported note ("Cindy", 7 rounds) legitimately produces."""
    entries: list[dict] = [
        {"movement": "Row", "set_number": 1, "reps": None, "weight": None, "duration": 180.0, "note": ""},
    ]
    entries += [
        {"movement": "Snatch", "set_number": n, "reps": 2, "weight": 30.0, "duration": None, "note": "high hang"}
        for n in range(1, 7)
    ]
    for rnd in range(1, 8):
        entries.append(
            {"movement": "Pull-up", "set_number": rnd, "reps": 5, "weight": None, "duration": None, "note": ""}
        )
        entries.append(
            {"movement": "Push-up", "set_number": rnd, "reps": 10, "weight": None, "duration": None, "note": ""}
        )
        entries.append(
            {"movement": "Air Squat", "set_number": rnd, "reps": 15, "weight": None, "duration": None, "note": ""}
        )
    return {"entries": entries, "unmatched": []}


def _truncate_mid_entry(payload: dict, after_entries: int) -> str:
    """Serialise `payload` pretty-printed, then cut it off part-way through the
    entry at index `after_entries` - exactly where the prod response stopped
    (immediately after a "set_number" value, with no closing brace)."""
    text = json.dumps(payload, indent=2)
    # Walk to the (after_entries + 1)-th "movement" key, then cut at the end of
    # that entry's "set_number" line - no closing brace, exactly as prod stopped.
    idx = -1
    for _ in range(after_entries + 1):
        idx = text.index('"movement":', idx + 1)
    return text[: text.index("\n", text.index('"set_number":', idx))]


def test_prod_shape_truncation_is_repaired():
    """The reported failure: truncated three levels deep. Must yield a dict."""
    truncated = _truncate_mid_entry(_cindy_payload(), after_entries=25)
    assert not truncated.rstrip().endswith("}"), "the fixture must really be truncated"
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)

    result = parse_andy_response(truncated)
    assert isinstance(result, dict)
    assert "entries" in result


def test_repair_keeps_every_complete_entry():
    """Salvage must not throw away the movements that did arrive intact - that
    loss is the whole user-visible symptom ("got useless junk")."""
    payload = _cindy_payload()
    truncated = _truncate_mid_entry(payload, after_entries=25)
    result = parse_andy_response(truncated)

    recovered = result["entries"]
    # The 25 entries before the cut must all survive, byte-identical.
    assert recovered[:25] == payload["entries"][:25]
    assert len(recovered) >= 25


def test_repair_preserves_the_partially_emitted_movement():
    """The entry the cut landed inside still names its movement. Surfacing it
    (numbers missing, for the user to fill in) beats dropping it silently -
    the same rule the parser already applies to an uncoercible row."""
    truncated = _truncate_mid_entry(_cindy_payload(), after_entries=25)
    result = parse_andy_response(truncated)
    assert result["entries"][25]["movement"] == "Pull-up"


def test_truncation_inside_a_string_value_is_repaired():
    """A cut mid-string, one level deeper than the old '\"}' suffix could reach."""
    truncated = '{\n  "entries": [\n    {"movement": "Row", "note": "3 min war'
    result = parse_andy_response(truncated)
    assert result["entries"][0]["movement"] == "Row"


def test_truncation_mid_key_falls_back_to_the_last_complete_pair():
    """A cut inside a KEY cannot be closed by quoting it - there is no value.
    The repair must step back to the previous structural boundary instead."""
    result = parse_andy_response('{"a": 1, "b')
    assert result == {"a": 1}


@pytest.mark.parametrize(
    "text, expected",
    [
        # Regressions: the shapes the pre-existing one-level repair handled must
        # keep working.
        ('{"andy_body_desc": "x"', {"andy_body_desc": "x"}),
        # A cut mid-VALUE keeps the partial string (what the old '"}' suffix did):
        # a truncated free-text note is harmless, losing the field is not.
        ('{"a": 1, "b": "tex', {"a": 1, "b": "tex"}),
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('{"a": 1}\n}\n', {"a": 1}),
    ],
)
def test_existing_shapes_still_parse(text, expected):
    assert parse_andy_response(text) == expected


@pytest.mark.parametrize("bad", ["", "   \n  ", "no json here at all", "[1, 2, 3]"])
def test_unsalvageable_input_still_raises(bad):
    """Repair must not paper over input that carries no object at all - the
    error message is the only diagnostic the confirm screen can show."""
    with pytest.raises(ValueError):
        parse_andy_response(bad)


def test_repair_does_not_invent_an_empty_object():
    """'{' alone is repairable to '{}' - but an empty dict is indistinguishable
    from 'the model returned nothing useful' and must not be reported as success."""
    with pytest.raises(ValueError):
        parse_andy_response("{")


def test_repair_attempts_are_bounded():
    """Each attempt re-parses the response, so unbounded attempts are quadratic.

    The input below is valid, then malformed halfway, then carries thousands more
    structural boundaries. raw_decode stops at the first syntax error, so every
    candidate cut after the halfway point costs the full walk up to it. Measured
    unbounded on a 32 KB body: 1.9s, growing as the square - and max_tokens=16384
    permits roughly twice that length. Bounded: 0.02s.

    Returning None here rather than a repair is the point, not a regression. What
    the unbounded scan eventually finds is a cut that silently discards half the
    response to route around a mid-document error - which is not a truncation at
    all. The diagnostic ValueError is the honest answer, and the caller already
    keeps the user's note and offers manual entry.
    """
    import time

    degenerate = '{"a":[' + "1," * 8000 + "@" + "1," * 8000
    started = time.monotonic()
    with pytest.raises(ValueError):
        parse_andy_response(degenerate)
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, f"repair must not degrade to a quadratic scan (took {elapsed:.2f}s)"


def test_bounding_does_not_break_a_realistic_repair():
    """The cap is measured from the truncation point backwards, so the salvage a
    real (long) response needs must still be found."""
    truncated = _truncate_mid_entry(_cindy_payload(), after_entries=25)
    assert len(parse_andy_response(truncated)["entries"]) == 26


def test_repair_is_logged():
    """A salvaged parse is a degraded parse: it must leave a trace."""
    import logging

    truncated = _truncate_mid_entry(_cindy_payload(), after_entries=25)
    logger = logging.getLogger("app.services.llm")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        parse_andy_response(truncated)
    finally:
        logger.removeHandler(handler)
    assert any("truncated" in r.getMessage().lower() for r in records)
