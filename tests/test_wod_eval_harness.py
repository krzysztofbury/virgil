"""The WOD eval harness (#155), end to end without a provider.

Answers come from LiteLLM's own mock_response, so the real parse_wod, call_llm,
cost lookup and report writer all run; only the network does not.
"""

import asyncio
import json

import litellm
import pytest

from evals.wod.run import load_cases, run_suite, write_report

MODEL = "openai/gpt-4o-mini"
CASES = [case for case in load_cases() if case["id"] in {"en-fran", "en-sled-oov", "pl-polish-names"}]
REAL_ACOMPLETION = litellm.acompletion


def _answer_for(note: str, mangle=None) -> str:
    case = next(case for case in CASES if case["note"] == note)
    payload = {
        "entries": [{**entry, "note": ""} for entry in case["expected"]["entries"]],
        "unmatched": case["expected"]["unmatched"],
    }
    text = json.dumps(payload)
    return mangle(case, text) if mangle else text


def _provider(mangle=None):
    async def respond(**kwargs):
        note = kwargs["messages"][1]["content"]
        return await REAL_ACOMPLETION(model=MODEL, messages=kwargs["messages"], mock_response=_answer_for(note, mangle))

    return respond


def _run(tmp_path, provider, budget=1.0, efforts=("low",)):
    configs = [(MODEL, effort, "sk-test") for effort in efforts]  # pragma: allowlist secret
    return asyncio.run(run_suite(CASES, configs, budget, tmp_path, provider))


def test_correct_answers_score_exact_and_reach_the_report(client, tmp_path):
    results, cut_short, spent = _run(tmp_path, _provider())
    (result,) = results
    assert not cut_short
    assert result["summary"]["case_exact"] == 1.0
    assert result["summary"]["json_valid"] == len(CASES)
    assert result["summary"]["cost_known_for"] == f"{len(CASES)}/{len(CASES)}" and spent > 0

    path = write_report(
        results,
        {
            "date": "2026-01-01",
            "commit": "abc",
            "cases": len(CASES),
            "budget_usd": 1.0,
            "spent_usd": spent,
            "cut_short": False,
            "configs": [],
        },
        tmp_path / "out",
    )
    report = path.read_text()
    assert f"| {MODEL} | low | 100% |" in report
    assert ": 0 of 3" in report, "no failing cases listed"
    assert json.loads(path.with_suffix(".json").read_text())["results"][0]["summary"]["case_exact"] == 1.0


def test_a_truncated_answer_is_repaired_and_still_scored(client, tmp_path):
    def cut(case, text):
        return text[:-30] if case["id"] == "en-fran" else text

    (result,), _cut, _spent = _run(tmp_path, _provider(cut))
    fran = next(run for run in result["runs"] if run["case_id"] == "en-fran")
    assert fran["json_health"] == "repaired"
    assert result["summary"]["json_repaired"] == 1


def test_a_mapped_unknown_movement_shows_as_hallucinated(client, tmp_path):
    def map_sled(case, text):
        if case["id"] != "en-sled-oov":
            return text
        payload = json.loads(text)
        payload["unmatched"] = []
        payload["entries"].append(
            {
                "movement": "Push Press",
                "set_number": 1,
                "reps": None,
                "weight": None,
                "duration": None,
                "equipment": None,
                "note": "",
            }
        )
        return json.dumps(payload)

    (result,), _cut, _spent = _run(tmp_path, _provider(map_sled))
    assert result["summary"]["oov_hallucinated"] == 1
    assert result["summary"]["case_exact"] == pytest.approx(2 / 3, abs=1e-4)


def test_the_budget_stops_the_run_before_the_next_call(client, tmp_path):
    calls = []
    inner = _provider()

    async def counting(**kwargs):
        calls.append(1)
        return await inner(**kwargs)

    results, cut_short, spent = _run(tmp_path, counting, budget=1e-9, efforts=("low", "medium"))
    assert cut_short and len(calls) == 1
    assert len(results) == 1 and len(results[0]["cases"]) == 1
    path = write_report(
        results,
        {
            "date": "2026-01-01",
            "commit": "abc",
            "cases": len(CASES),
            "budget_usd": 1e-9,
            "spent_usd": spent,
            "cut_short": True,
            "configs": [],
        },
        tmp_path / "out",
    )
    assert "cut short by the budget" in path.read_text()


def test_the_harness_leaves_litellm_as_it_found_it(client, tmp_path):
    _run(tmp_path, _provider())
    assert litellm.acompletion is REAL_ACOMPLETION
