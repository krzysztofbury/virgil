"""The WOD eval's scorer (#155): a scoring bug must not pass as a model failure."""

import json

from evals.wod.scoring import aggregate, json_health, off_list_names, score_case


def _entry(movement, set_number, reps=None, weight=None, duration=None, equipment=None):
    return {
        "movement": movement,
        "set_number": set_number,
        "reps": reps,
        "weight": weight,
        "duration": duration,
        "equipment": equipment,
    }


CASE = {
    "id": "fran",
    "tags": ["english"],
    "note": "21-15 thrusters 43kg, sled push",
    "expected": {
        "entries": [_entry("Thruster", 1, 21, 43.0), _entry("Thruster", 2, 15, 43.0)],
        "unmatched": ["sled push"],
    },
}


def test_a_perfect_parse_is_exact():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], ["Sled  Push"])
    assert score.case_exact and score.exact_entries == 2 and score.oov_rejected == 1
    assert score.details == []


def test_a_wrong_weight_counts_against_that_field_only():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 40), _entry("Thruster", 2, 15, 43)], ["sled push"])
    assert not score.case_exact
    assert score.field_hits == {"reps": 2, "weight": 1, "duration": 2, "equipment": 2}
    assert score.exact_entries == 1
    assert score.details == ["Thruster set 1: weight 40 != 43.0"]


def test_a_missing_set_and_an_invented_number():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 43, duration=60)], ["sled push"])
    assert score.paired == 1 and score.returned_entries == 1
    assert score.invented_values == 1, "duration was empty in the expected entry"
    assert "missed Thruster set 2" in score.details


def test_an_unknown_movement_mapped_to_a_known_one_is_hallucinated():
    returned = [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43), _entry("Push Press", 1)]
    score = score_case(CASE, returned, [])
    assert (score.oov_rejected, score.oov_hallucinated, score.oov_dropped) == (0, 1, 0)
    assert "extra Push Press set 1" in score.details


def test_an_unknown_movement_that_vanishes_is_dropped():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], [])
    assert (score.oov_rejected, score.oov_hallucinated, score.oov_dropped) == (0, 0, 1)


def test_a_failed_parse_scores_nothing_and_keeps_its_error():
    score = score_case(CASE, [], [], error="ValueError: no JSON")
    assert not score.case_exact and score.paired == 0
    assert score.details[0] == "error: ValueError: no JSON"


def test_json_health_tells_valid_repaired_and_unusable_apart():
    whole = json.dumps({"entries": [{"movement": "Thruster", "set_number": 1}], "unmatched": []})
    assert json_health(whole) == "valid"
    assert json_health(whole[:-3]) == "repaired", "cut mid-object, as a truncated response is"
    assert json_health("I could not read that note.") == "unusable"
    assert json_health(None) == "unusable"


def test_off_list_names_counts_what_the_parser_had_to_move():
    raw = json.dumps({"entries": [{"movement": "thruster"}, {"movement": "Sled Push"}], "unmatched": []})
    assert off_list_names(raw, {"Thruster"}) == 1


def test_aggregate_rates_and_costs():
    perfect = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], ["sled push"])
    half = score_case(CASE, [_entry("Thruster", 1, 21, 40)], [])
    runs = [
        {"latency_s": 1.0, "cost_usd": 0.002, "json_health": "valid"},
        {"latency_s": 3.0, "cost_usd": None, "json_health": "repaired"},
    ]
    row = aggregate([perfect, half], runs)
    assert row["case_exact"] == 0.5
    assert row["recall"] == 0.75, "3 of 4 expected entries paired"
    assert row["precision"] == 1.0
    assert row["entry_exact"] == 0.5, "2 of 4 expected entries exactly right"
    assert row["weight_accuracy"] == round(2 / 3, 4)
    assert row["oov_rejected"] == 0.5
    assert (row["json_valid"], row["json_repaired"]) == (1, 1)
    assert row["latency_p50_s"] == 2.0
    assert row["cost_per_100_usd"] == 0.2 and row["cost_known_for"] == "1/2"


def test_an_unrelated_extra_entry_does_not_make_a_dropped_name_hallucinated():
    returned = [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43), _entry("Thruster", 3, 9, 43)]
    score = score_case(CASE, returned, [])
    assert (score.oov_rejected, score.oov_hallucinated, score.oov_dropped) == (0, 0, 1)


def test_a_rejected_name_reported_with_its_numbers_still_counts_as_rejected():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], ["Sled push 4 x 20m"])
    assert score.oov_rejected == 1 and score.unexpected_unmatched == []
    assert score.case_exact


def test_a_rejected_name_must_appear_as_whole_words():
    score = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], ["sled pushes"])
    assert score.oov_rejected == 0 and score.unexpected_unmatched == ["sled pushes"]


def test_json_wrapped_in_a_fence_or_prose_is_extracted_not_repaired():
    whole = json.dumps({"entries": [], "unmatched": []})
    assert json_health(f"```json\n{whole}\n```") == "extracted"
    assert json_health(f"Here you go: {whole}") == "extracted"
    assert json_health(f"```json\n{whole[:-3]}") == "repaired"


def test_ambiguous_cases_are_kept_out_of_the_headline_rate():
    ambiguous = {**CASE, "id": "fran-ambiguous", "tags": ["ambiguous"]}
    clear = score_case(CASE, [_entry("Thruster", 1, 21, 43), _entry("Thruster", 2, 15, 43)], ["sled push"])
    missed = score_case(ambiguous, [], ["sled push"])
    row = aggregate([clear, missed], [{"json_health": "valid"}, {"json_health": "valid"}])
    assert row["case_exact"] == 1.0 and row["ambiguous_exact"] == 0.0
