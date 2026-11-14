"""The WOD eval harness (#155), end to end without a provider.

Answers come from LiteLLM's own mock_response, so the real parse_wod, call_llm,
cost lookup and report writer all run; only the network does not.
"""

import asyncio
import json
import os
import subprocess
import sys

import litellm
import pytest

from evals.wod.run import (
    ROOT,
    UnpricedModelError,
    commit_state,
    isolate,
    key_for,
    load_cases,
    load_prices,
    refusal,
    render_markdown,
    run_suite,
    write_report,
)

MODEL = "openai/gpt-4o-mini"
ALL_CASES = load_cases()
CASES = [case for case in ALL_CASES if case["id"] in {"en-fran", "en-sled-oov", "pl-polish-names"}]
REAL_ACOMPLETION = litellm.acompletion


def _answer_for(note: str, mangle=None) -> str:
    case = next(case for case in ALL_CASES if case["note"] == note)
    payload = {
        "entries": [{**entry, "note": ""} for entry in case["expected"]["entries"]],
        "unmatched": case["expected"]["unmatched"],
    }
    text = json.dumps(payload)
    return mangle(case, text) if mangle else text


def _provider(mangle=None, model=MODEL):
    async def respond(**kwargs):
        note = kwargs["messages"][1]["content"]
        return await REAL_ACOMPLETION(model=model, messages=kwargs["messages"], mock_response=_answer_for(note, mangle))

    return respond


def _run(tmp_path, provider, budget=1.0, efforts=("low",), allow_unpriced=False, model=MODEL, cases=CASES):
    configs = [(model, effort, "sk-test") for effort in efforts]  # pragma: allowlist secret
    return asyncio.run(run_suite(cases, configs, budget, tmp_path, provider, allow_unpriced=allow_unpriced))


META = {"date": "2026-01-01", "commit": "abc", "cases": 3, "budget_usd": 1.0, "spent_usd": 0.0, "cut_short": False}


def test_correct_answers_score_exact_and_reach_the_report(client, tmp_path):
    results, cut_short, spent, skipped = _run(tmp_path, _provider())
    (result,) = results
    assert skipped == []
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
    assert f"| {MODEL} | low | 3 of 3 | 100% |" in report
    assert ": 0 of 3" in report, "no failing cases listed"
    assert json.loads(path.with_suffix(".json").read_text())["results"][0]["summary"]["case_exact"] == 1.0


def test_a_truncated_answer_is_repaired_and_still_scored(client, tmp_path):
    def cut(case, text):
        return text[:-30] if case["id"] == "en-fran" else text

    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider(cut))
    fran = next(run for run in result["runs"] if run["case_id"] == "en-fran")
    assert fran["json_health"] == "repaired"
    assert result["summary"]["json_repaired"] == 1


def test_a_mapped_unknown_movement_shows_as_hallucinated(client, tmp_path):
    """The parser refuses a movement the note never writes (#187), but a mapped
    name still gets through when the note writes the movement inside another
    one: "hang snatch" writes Snatch. The harness must count that."""

    def map_sled(case, text):
        if case["id"] != "en-sled-oov":
            return text
        payload = json.loads(text)
        payload["unmatched"] = []
        payload["entries"].append(
            {
                "movement": "Snatch",
                "set_number": 1,
                "reps": None,
                "weight": None,
                "duration": None,
                "equipment": None,
                "note": "",
            }
        )
        return json.dumps(payload)

    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider(map_sled))
    assert result["summary"]["oov_hallucinated"] == 1
    assert result["summary"]["case_exact"] == pytest.approx(2 / 3, abs=1e-4)


def test_the_budget_stops_the_run_before_the_next_call(client, tmp_path):
    calls = []
    inner = _provider()

    async def counting(**kwargs):
        calls.append(1)
        return await inner(**kwargs)

    results, cut_short, spent, skipped = _run(tmp_path, counting, budget=1e-9, efforts=("low", "medium"))
    assert cut_short and len(calls) == 1
    assert len(results) == 1 and len(results[0]["cases"]) == 1
    assert skipped == [[MODEL, "medium", "budget reached"]], "the medium config scored nothing and has no row"
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
            "skipped": skipped,
        },
        tmp_path / "out",
    )
    report = path.read_text()
    assert "cut short by the budget" in report
    assert f"| {MODEL} | low | 1 of 3 |" in report, "a partial row says how many cases it holds"
    assert f"| {MODEL} | medium |" not in report
    assert f"- {MODEL}, effort medium: not run, budget reached." in report


def test_a_config_the_budget_stops_before_its_first_case_gets_no_row(client, tmp_path):
    """The budget ran out at a config boundary: the next config scored nothing,
    and an empty row would read as a model that failed every case."""
    results, cut_short, _spent, skipped = _run(tmp_path, _provider(), budget=0, efforts=("low", "medium"))
    assert cut_short and results == []
    assert skipped == [[MODEL, "low", "budget reached"], [MODEL, "medium", "budget reached"]]


def test_the_harness_leaves_litellm_as_it_found_it(client, tmp_path):
    _run(tmp_path, _provider())
    assert litellm.acompletion is REAL_ACOMPLETION


# gemini-3-pro-preview is priced at 0 in LiteLLM's bundled map; the other is not in it at all.
UNPRICED = ["gemini/gemini-3-pro-preview", "openai/not-in-the-cost-map"]


@pytest.mark.parametrize("model", UNPRICED)
def test_an_unpriced_model_stops_the_run_after_one_call(client, tmp_path, model):
    """A budget cap that counts nothing is no cap: the run must not go on blind."""
    calls = []
    inner = _provider(model=model)

    async def counting(**kwargs):
        calls.append(1)
        return await inner(**kwargs)

    with pytest.raises(UnpricedModelError, match="--allow-unpriced"):
        _run(tmp_path, counting)
    assert len(calls) == 1
    assert litellm.acompletion is REAL_ACOMPLETION


@pytest.mark.parametrize("model", UNPRICED)
def test_an_unpriced_model_runs_when_allowed_and_says_its_cost_is_unknown(client, tmp_path, model):
    (result,), cut_short, spent, _skipped = _run(tmp_path, _provider(model=model), allow_unpriced=True)
    assert not cut_short and spent == 0
    assert result["summary"]["cost_known_for"] == f"0/{len(CASES)}"
    assert result["summary"]["cost_per_100_usd"] is None


def test_the_report_shows_seconds_and_dollars_as_numbers(client, tmp_path):
    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider())
    result["summary"].update(case_exact=0.5, latency_p50_s=0.85, latency_p95_s=0.97, cost_per_100_usd=0.011)
    row = next(line for line in render_markdown([result], META).splitlines() if line.startswith(f"| {MODEL}"))
    assert "| 50% |" in row
    assert "| 0.85 | 0.97 | 0.011 |" in row


@pytest.fixture
def registered_prices():
    """load_prices writes into LiteLLM's global cost map; take its entries out
    again so the unpriced tests above stay unpriced."""
    before = {key: dict(value) for key, value in litellm.model_cost.items() if isinstance(value, dict)}
    keys = set(litellm.model_cost)
    yield
    for key in set(litellm.model_cost) - keys:
        del litellm.model_cost[key]
    for key, value in before.items():
        if litellm.model_cost.get(key) != value:
            litellm.model_cost[key] = value
    litellm.utils._invalidate_model_cost_lowercase_map()


def test_a_price_from_the_command_line_prices_an_unmapped_model(client, tmp_path, registered_prices):
    model = "openai/not-in-the-cost-map"
    source = load_prices("bundled", [f"{model}=1/2"])
    assert source["command_line"] == {model: {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}}
    assert source["map"].startswith("bundled with LiteLLM ")

    (result,), _cut, spent, _skipped = _run(tmp_path, _provider(model=model), model=model)
    assert spent > 0 and result["summary"]["cost_known_for"] == f"{len(CASES)}/{len(CASES)}"
    report = render_markdown([result], {**META, "prices": source})
    assert f"`{model}` from the command line, 1.0 USD in and 2.0 USD out per million tokens." in report


def test_a_command_line_price_overrides_the_map_also_for_a_dated_answer(client, tmp_path, registered_prices):
    """Providers answer with a dated snapshot name; the price typed for the
    model asked for must still be the one charged, not the map's."""
    load_prices("bundled", [f"{MODEL}=100/200"])
    inner = _provider()

    async def dated(**kwargs):
        response = await inner(**kwargs)
        response.model = "gpt-4o-mini-2024-07-18"
        return response

    (result,), _cut, spent, _skipped = _run(tmp_path, dated)
    run = result["runs"][0]
    assert run["response_model"] == "gpt-4o-mini-2024-07-18"
    assert run["cost_usd"] == pytest.approx((run["prompt_tokens"] * 100 + run["output_tokens"] * 200) / 1e6, rel=1e-9)


def test_the_published_cost_map_prices_a_model_the_bundled_one_lacks(client, tmp_path, registered_prices):
    model = "openai/not-in-the-cost-map"
    published = {
        "sample_spec": {"input_cost_per_token": "fill"},
        "not-in-the-cost-map": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 2e-6,
            "litellm_provider": "openai",
            "mode": "chat",
        },
    }
    source = load_prices("published", [], fetch=lambda url: json.dumps(published).encode())
    assert source["map"].startswith("published at https://") and "sha256 " in source["map"]
    (result,), _cut, spent, _skipped = _run(tmp_path, _provider(model=model), model=model)
    assert spent > 0 and result["summary"]["cost_per_100_usd"] is not None


@pytest.mark.parametrize("body", [b"<html>rate limited</html>", b"[]", None])
def test_a_published_map_that_does_not_load_stops_the_run(registered_prices, body):
    def fetch(url):
        if body is None:
            raise OSError("network unreachable")
        return body

    with pytest.raises(SystemExit, match="Could not load the published cost map"):
        load_prices("published", [], fetch=fetch)


@pytest.mark.parametrize("spec", ["openai/x", "openai/x=1", "openai/x=a/b", "openai/x=-1/2", "x=1/2"])
def test_a_malformed_price_is_refused(spec):
    with pytest.raises(SystemExit):
        load_prices("bundled", [spec])


def test_each_run_records_the_effort_sent_and_the_model_that_answered(client, tmp_path):
    """Gemini takes no xhigh, so call_llm steps it down; the report must show
    the effort that went out, not the one asked for."""
    model = "gemini/gemini-2.5-flash"
    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider(model=model), efforts=("xhigh",), model=model)
    assert {run["effort_sent"] for run in result["runs"]} == {"high"}
    assert result["summary"]["effort_dropped"] == 0
    assert result["summary"]["efforts_sent"] == ["high"]
    assert result["summary"]["response_models"] == ["gemini-2.5-flash"]
    report = render_markdown([result], META)
    assert f"- {model}, effort xhigh: sent `high`, answered by `gemini-2.5-flash`, 0 errors" in report


def test_an_effort_litellm_drops_is_not_reported_as_sent(client, tmp_path):
    """gpt-4o-mini takes no reasoning_effort, and call_llm's drop_params removes
    it, so a low and a medium row would be the same request."""
    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider())
    assert result["summary"]["efforts_sent"] == [] and result["summary"]["effort_dropped"] == len(CASES)
    assert f"sent none, dropped by LiteLLM in {len(CASES)} calls" in render_markdown([result], META)


def test_an_unknown_effort_is_refused():
    from evals.wod.run import check_efforts

    check_efforts(["none", "low", "xhigh"])
    with pytest.raises(SystemExit, match="Unknown effort hihg"):
        check_efforts(["low", "hihg"])


def test_a_refused_key_stops_that_model_and_leaves_nothing_to_report(client, tmp_path):
    calls = []

    async def refused(**kwargs):
        calls.append(1)
        raise litellm.AuthenticationError(message="invalid api key", llm_provider="openai", model=MODEL)

    results, cut_short, spent, skipped = _run(tmp_path, refused, efforts=("low", "medium"))
    assert len(calls) == 1, "one refusal is enough: the same key would be refused for every case and effort"
    assert results == [] and not cut_short and spent == 0
    assert skipped == [[MODEL, "low", "key refused by the provider"], [MODEL, "medium", "key refused by the provider"]]
    assert refusal(results) == "no case was scored"


def test_a_timed_out_call_counts_against_the_budget_at_its_worst_case(client, tmp_path):
    inner = _provider()

    async def timing_out(**kwargs):
        if "Fran" in kwargs["messages"][1]["content"]:
            raise litellm.Timeout(message="read timed out", model=MODEL, llm_provider="openai")
        return await inner(**kwargs)

    (result,), _cut, spent, _skipped = _run(tmp_path, timing_out)
    fran = next(run for run in result["runs"] if run["case_id"] == "en-fran")
    assert fran["cost_estimated"] and fran["json_health"] == "no_response"
    # gpt-4o-mini output costs 0.6 USD per million tokens, and parse_wod allows 16384.
    assert fran["cost_usd"] >= 16384 * 0.6e-6
    assert spent >= fran["cost_usd"]
    assert result["summary"]["cost_estimated"] == 1
    assert result["summary"]["cost_known_for"] == f"{len(CASES) - 1}/{len(CASES)}", "a worst case is not a price"
    assert result["summary"]["json_no_response"] == 1 and result["summary"]["json_unusable"] == 0


def test_a_timed_out_call_on_an_unpriced_model_stops_the_run(client, tmp_path):
    model = "openai/not-in-the-cost-map"

    async def timing_out(**kwargs):
        raise litellm.Timeout(message="read timed out", model=model, llm_provider="openai")

    with pytest.raises(UnpricedModelError):
        _run(tmp_path, timing_out, model=model)


def test_an_error_without_a_response_is_reported_without_em_dashes(client, tmp_path):
    inner = _provider()

    async def limited(**kwargs):
        if "Fran" in kwargs["messages"][1]["content"]:
            raise litellm.RateLimitError(message="slow down", llm_provider="openai", model=MODEL)
        return await inner(**kwargs)

    (result,), _cut, _spent, _skipped = _run(tmp_path, limited)
    fran = next(case for case in result["cases"] if case["case_id"] == "en-fran")
    assert fran["error"].startswith("ValueError: LLM rate limit exceeded for model")
    assert "\u2014" not in fran["error"] and "\u2013" not in fran["error"]
    assert next(run for run in result["runs"] if run["case_id"] == "en-fran")["json_health"] == "no_response"
    assert "\u2014" not in render_markdown([result], META)


def test_no_report_when_every_case_errored(client, tmp_path):
    async def failing(**kwargs):
        raise litellm.RateLimitError(message="slow down", llm_provider="openai", model=MODEL)

    results, _cut, _spent, _skipped = _run(tmp_path, failing)
    assert len(results) == 1 and results[0]["summary"]["errors"] == len(CASES)
    assert refusal(results).startswith("every case errored, the first with ValueError: LLM rate limit exceeded")

    (good,), _cut, _spent, _skipped = _run(tmp_path, _provider())
    assert refusal([*results, good]) is None


def test_aliases_are_not_counted_as_off_list_names(client, tmp_path):
    def by_alias(case, text):
        return text.replace('"Pull-up"', '"pull-ups"') if case["id"] == "en-fran" else text

    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider(by_alias))
    fran = next(run for run in result["runs"] if run["case_id"] == "en-fran")
    assert '"pull-ups"' in fran["raw"]
    assert fran["off_list_names"] == 0
    assert result["summary"]["case_exact"] == 1.0


@pytest.mark.parametrize("model", ["gpt-4o-mini", "/gpt-4o-mini", "openai/"])
def test_a_model_without_a_provider_prefix_is_refused(model):
    with pytest.raises(SystemExit, match="provider prefix"):
        key_for(model, environ={"OPENAI_API_KEY": "sk-test"})  # pragma: allowlist secret


def test_isolate_points_the_app_at_the_scratch_directory(tmp_path):
    """In a fresh interpreter, as the harness runs: app.config reads the
    scratch path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("VIRGIL_")}
    script = (
        "import sys; from pathlib import Path; from evals.wod.run import isolate; "
        "isolate(Path(sys.argv[1])); from app import config; print(config.CENTRAL_DB_PATH)"
    )
    out = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, env=env, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == str(tmp_path / "central.db")


def test_isolate_refuses_an_app_already_pointed_elsewhere(client, tmp_path, monkeypatch):
    """Under pytest app.config is imported with the test suite's path, as it
    would be when anything imported the app before isolate() ran."""
    monkeypatch.setenv("VIRGIL_CENTRAL_DB_PATH", os.environ["VIRGIL_CENTRAL_DB_PATH"])
    monkeypatch.setenv("VIRGIL_ENCRYPTION_KEY", os.environ.get("VIRGIL_ENCRYPTION_KEY", ""))
    with pytest.raises(RuntimeError, match="outside the scratch directory"):
        isolate(tmp_path)


def test_a_dirty_tree_is_flagged_in_the_report(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "f.txt").write_text("a")
    git("add", "f.txt")
    git("commit", "-q", "-m", "first")
    commit, dirty = commit_state(tmp_path)
    assert len(commit) >= 7 and not dirty
    (tmp_path / "f.txt").write_text("b")
    assert commit_state(tmp_path) == (commit, True)

    report = render_markdown([], {**META, "commit": commit, "dirty": True})
    assert f"Commit `{commit}` with uncommitted changes," in report


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["id"])
def test_every_golden_answer_survives_the_parser(client, tmp_path, case):
    """The expected output, returned by a model word for word, must score exact
    through the real parser: a case its controls would refuse is a wrong case."""
    (result,), _cut, _spent, _skipped = _run(tmp_path, _provider(), cases=[case])
    assert result["cases"][0]["case_exact"], result["cases"][0]["details"]
