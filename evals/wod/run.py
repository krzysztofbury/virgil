"""Live evaluation of the WOD parser (#155). It makes paid model calls.

    uv run --locked --extra dev python -m evals.wod.run \\
        --models anthropic/claude-haiku-4-5,gemini/gemini-3-flash-preview \\
        --efforts low,medium --budget-usd 2

Every case goes through the real parse_wod and call_llm, against a throwaway,
freshly migrated database whose seeded vocabulary the expected outputs are
written for. Models are given with their LiteLLM provider prefix. Provider keys
come from the usual environment variables (ANTHROPIC_API_KEY, OPENAI_API_KEY,
GEMINI_API_KEY...), never from an account.

Prices come from LiteLLM's bundled cost map unless --cost-map published loads
the current published one, and --price MODEL=IN/OUT sets a model's price in USD
per million input and output tokens; the report names the source. The run stops
before the next call once the measured cost reaches the budget, and stops at
once on a model it has no price for, since the budget could not stop it;
--allow-unpriced runs such a model anyway, with cost unknown. A call that timed
out may still be billed, so it is charged at its worst case (the whole output
allowance). A model whose key is refused is not run further. Results go to
docs/evals/wod-YYYY-MM-DD.md and .json, and no report is written when every
case errored.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
CASES = Path(__file__).with_name("cases.jsonl")
KEY_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "groq": "GROQ_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
# The map LiteLLM publishes; the bundled one is a snapshot of it from the release.
PUBLISHED_COST_MAP_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def provider_of(model: str) -> str:
    """The provider prefix of a LiteLLM model string. An unprefixed model is
    refused: LiteLLM guesses its provider from the name, and the key, the stored
    provider and the price would each have to make the same guess."""
    provider, sep, name = model.partition("/")
    if not sep or not provider or not name:
        raise SystemExit(f"Give {model!r} with its provider prefix, for example openai/{model or 'gpt-4o-mini'}")
    return provider


def key_for(model: str, environ=os.environ) -> str:
    name = KEY_VARS.get(provider_of(model))
    if not name or not environ.get(name):
        raise SystemExit(f"No key for {model}: set {name or 'the provider key variable'}")
    return environ[name]


def isolate(scratch: Path) -> None:
    """Point the app at throwaway storage before any app module is imported, and
    refuse to go on when the app's central database is anywhere else: a .env or
    an app module imported earlier would otherwise send the run to real data."""
    from cryptography.fernet import Fernet

    os.environ["VIRGIL_CENTRAL_DB_PATH"] = str(scratch / "central.db")
    os.environ["VIRGIL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    from app import config

    if not Path(config.CENTRAL_DB_PATH).resolve().is_relative_to(scratch.resolve()):
        raise RuntimeError(
            f"The app's central database {config.CENTRAL_DB_PATH} is outside the scratch directory {scratch}"
        )


def _fetch_published(url: str) -> bytes:
    import httpx

    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    return response.content


def parse_price(spec: str) -> tuple[str, float, float]:
    """MODEL=IN/OUT, both in USD per million tokens."""
    model, sep, rates = spec.partition("=")
    rate_in, slash, rate_out = rates.partition("/")
    try:
        prices = float(rate_in), float(rate_out)
    except ValueError:
        prices = None
    if not sep or not slash or prices is None or min(prices) < 0 or not model.strip():
        raise SystemExit(
            f"--price takes MODEL=IN/OUT in USD per million tokens, for example openai/x=0.4/1.6: {spec!r}"
        )
    provider_of(model.strip())
    return model.strip(), *prices


def load_prices(cost_map: str, prices: list[str], fetch=_fetch_published) -> dict:
    """Load the prices the run will charge by, and say where they came from.

    Call after isolate(): LiteLLM reads its bundled map when it is imported.
    The published map is loaded or the run stops; a silent fall back to the
    bundled one would leave the report naming the wrong source.
    """
    from importlib.metadata import version

    import litellm

    source: dict = {"map": f"bundled with LiteLLM {version('litellm')}"}
    if cost_map == "published":
        try:
            body = fetch(PUBLISHED_COST_MAP_URL)
            published = json.loads(body)
            if not isinstance(published, dict):
                raise ValueError("not a JSON object")
        except Exception as exc:  # noqa: BLE001 - any failure to load means no published prices
            raise SystemExit(f"Could not load the published cost map from {PUBLISHED_COST_MAP_URL}: {exc}") from exc
        entries = {
            key: value
            for key, value in published.items()
            if isinstance(value, dict) and key not in {"sample_spec", "fallback_generalizations"}
        }
        litellm.register_model(entries, persist_across_reloads=False)
        source["map"] = (
            f"published at {PUBLISHED_COST_MAP_URL}, fetched {datetime.now(UTC).isoformat(timespec='seconds')}, "
            f"sha256 {hashlib.sha256(body).hexdigest()[:12]}"
        )
    source["command_line"] = {}
    for spec in prices:
        model, rate_in, rate_out = parse_price(spec)
        litellm.register_model(
            {
                model: {
                    "input_cost_per_token": rate_in / 1e6,
                    "output_cost_per_token": rate_out / 1e6,
                    "litellm_provider": provider_of(model),
                    "mode": "chat",
                }
            },
            persist_across_reloads=False,
        )
        source["command_line"][model] = {"input_usd_per_mtok": rate_in, "output_usd_per_mtok": rate_out}
    return source


_DASHES = {0x2014: "-", 0x2013: "-"}


def plain(text: str) -> str:
    """The app's error messages use em dashes; the reports do not."""
    return text.translate(_DASHES)


def check_efforts(efforts: list[str]) -> None:
    """Call after isolate(), which the import needs. A stored effort outside the
    list falls back to the default, so a typo would run as "medium" under the
    label the user typed."""
    from app.services.llm import REASONING_EFFORTS

    unknown = [effort for effort in efforts if effort not in REASONING_EFFORTS]
    if unknown:
        raise SystemExit(f"Unknown effort {', '.join(unknown)}: use one of {', '.join(REASONING_EFFORTS)}")


class UnpricedModelError(RuntimeError):
    pass


def worst_case_cost(request: dict) -> float | None:
    """What a call that never answered can have cost at most: its whole prompt
    and its whole output allowance. None when the model has no price."""
    import litellm

    model = request.get("model") or ""
    try:
        prompt_tokens = litellm.token_counter(model=model, messages=request.get("messages") or [])
        prompt_cost, output_cost = litellm.cost_per_token(
            model=model, prompt_tokens=prompt_tokens, completion_tokens=int(request.get("max_tokens") or 0)
        )
    except Exception:  # noqa: BLE001 - no price is answered as unknown, like a priced call's lookup
        return None
    return (prompt_cost + output_cost) or None


class Meter:
    """Wraps litellm.acompletion for the run: latency, cost, the effort sent,
    the model that answered and the raw text of each call, read by the harness
    after parse_wod returns."""

    def __init__(self, inner):
        self.inner = inner
        self.last: dict | None = None
        self.spent = 0.0
        self.auth_failed = False

    async def __call__(self, *args, **kwargs):
        import litellm

        started = time.perf_counter()
        sent = {"effort_sent": kwargs.get("reasoning_effort"), "response_model": None}
        try:
            response = await self.inner(*args, **kwargs)
        except litellm.AuthenticationError:
            self.auth_failed = True
            raise
        except litellm.RateLimitError:
            raise
        except litellm.Timeout, litellm.APIError:
            # call_llm reports these as LLMCallAmbiguousError: the provider may
            # have accepted, and billed, the request before the transport failed.
            cost = worst_case_cost(kwargs)
            self.last = {
                "latency_s": round(time.perf_counter() - started, 3),
                "cost_usd": cost,
                "cost_estimated": True,
                "raw": None,
                "responded": False,
                "finish_reason": "",
                "output_tokens": None,
                **sent,
            }
            self.spent += cost or 0.0
            raise
        choice = response.choices[0]
        try:
            cost = litellm.completion_cost(completion_response=response)
        except Exception:  # noqa: BLE001 - a model without a price still gets scored
            cost = None
        # LiteLLM prices some preview models at 0, which would count as free.
        cost = cost or None
        usage = getattr(response, "usage", None)
        self.last = {
            "latency_s": round(time.perf_counter() - started, 3),
            "cost_usd": cost,
            "cost_estimated": False,
            "raw": choice.message.content,
            "responded": True,
            "finish_reason": str(getattr(choice, "finish_reason", "") or ""),
            "output_tokens": getattr(usage, "completion_tokens", None),
            **sent,
            "response_model": getattr(response, "model", None),
        }
        self.spent += cost or 0.0
        return response


class SuiteRun(NamedTuple):
    results: list[dict]
    cut_short: bool
    spent: float
    # Configs that scored no case: [model, effort, reason].
    skipped: list[list[str]]


_NO_CALL = {
    "latency_s": None,
    "cost_usd": None,
    "cost_estimated": False,
    "raw": None,
    "responded": False,
    "finish_reason": "",
    "output_tokens": None,
    "effort_sent": None,
    "response_model": None,
}


async def run_suite(
    cases: list[dict],
    configs: list[tuple[str, str, str]],
    budget_usd: float,
    scratch: Path,
    inner,
    allow_unpriced: bool = False,
) -> SuiteRun:
    """Score every case for every (model, effort, api_key). Raises
    UnpricedModelError after the first call whose cost is unknown, unless
    allow_unpriced. A refused key stops that model's configs."""
    import aiosqlite
    import litellm

    from app.db import set_setting
    from app.migrations.runner import run_migrations
    from app.services.encryption import encrypt
    from app.services.llm import REASONING_EFFORT_SETTING
    from app.services.wod_parser import canonical_movements, parse_wod
    from evals.wod.scoring import aggregate, json_health, off_list_names, score_case

    db = await aiosqlite.connect(scratch / "eval.db")
    db.row_factory = aiosqlite.Row
    await run_migrations(db)
    movements = await canonical_movements(db)
    # Aliases are allowed answers too: the parser maps them to their movement.
    vocabulary = {name for m in movements for name in [m["name"], *m["aliases"]]}

    meter = Meter(inner)
    original = litellm.acompletion
    litellm.acompletion = meter
    results, skipped, refused_models, cut_short = [], [], set(), False
    try:
        for model, effort, api_key in configs:
            if cut_short:
                skipped.append([model, effort, "budget reached"])
                continue
            if model in refused_models:
                skipped.append([model, effort, "key refused by the provider"])
                continue
            await db.execute("DELETE FROM llm_providers")
            await db.execute(
                "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 1)",
                (provider_of(model), encrypt(api_key), model),
            )
            await set_setting(db, REASONING_EFFORT_SETTING, effort)
            await db.commit()
            scores, runs = [], []
            meter.auth_failed = False
            for case in cases:
                if meter.spent >= budget_usd:
                    cut_short = True
                    break
                meter.last, entries, unmatched, error = None, [], [], None
                try:
                    parsed = await parse_wod(db, case["note"])
                    entries = [asdict(entry) for entry in parsed.entries]
                    unmatched = list(parsed.unmatched)
                except Exception as exc:  # noqa: BLE001 - a failed parse is a result, not a crash
                    error = plain(f"{type(exc).__name__}: {exc}")[:300]
                if meter.auth_failed:
                    refused_models.add(model)
                    break
                if meter.last and meter.last["cost_usd"] is None and not allow_unpriced:
                    raise UnpricedModelError(
                        f"{model} has no price in the cost map, so the budget cannot stop it. "
                        "Pass --price, --cost-map published or --allow-unpriced."
                    )
                run = dict(meter.last or _NO_CALL)
                run["json_health"] = json_health(run["raw"]) if run["responded"] else "no_response"
                run["off_list_names"] = off_list_names(run["raw"], vocabulary)
                run["case_id"] = case["id"]
                runs.append(run)
                scores.append(score_case(case, entries, unmatched, error))
            if model in refused_models:
                skipped.append([model, effort, "key refused by the provider"])
            elif not scores:
                skipped.append([model, effort, "budget reached"])
            else:
                results.append(
                    {
                        "model": model,
                        "effort": effort,
                        "summary": aggregate(scores, runs),
                        "cases": [asdict(s) for s in scores],
                        "runs": runs,
                    }
                )
    finally:
        litellm.acompletion = original
        await db.close()
    return SuiteRun(results, cut_short, round(meter.spent, 6), skipped)


def refusal(results: list[dict]) -> str | None:
    """Why the run must not be written up, or None. A report of nothing but
    errors would read as a model scoring zero."""
    cases = [case for result in results for case in result["cases"]]
    if not cases:
        return "no case was scored"
    if all(case["error"] for case in cases):
        return f"every case errored, the first with {cases[0]['error']}"
    return None


COLUMNS = [
    ("cases", "Cases"),
    ("case_exact", "Case exact"),
    ("ambiguous_exact", "Ambiguous exact"),
    ("entry_exact", "Entry exact"),
    ("recall", "Recall"),
    ("precision", "Precision"),
    ("reps_accuracy", "Reps"),
    ("weight_accuracy", "Weight"),
    ("duration_accuracy", "Duration"),
    ("equipment_accuracy", "Equipment"),
    ("oov_rejected", "OOV rejected"),
    ("invented_values", "Invented"),
    ("json_repaired", "Repaired"),
    ("json_unusable", "Unusable"),
    ("latency_p50_s", "p50 s"),
    ("latency_p95_s", "p95 s"),
    ("cost_per_100_usd", "USD / 100 notes"),
]


PERCENT = {"case_exact", "ambiguous_exact", "entry_exact", "recall", "precision", "oov_rejected"} | {
    f"{name}_accuracy" for name in ("reps", "weight", "duration", "equipment")
}


def _cell(key: str, value, total: int | None = None) -> str:
    if value is None:
        return "n/a"
    if key == "cases" and total is not None:
        return f"{value} of {total}"
    if key in PERCENT:
        return f"{value:.0%}"
    return str(value)


def _list(values) -> str:
    return ", ".join(f"`{value}`" for value in values) if values else "none"


def render_markdown(results: list[dict], meta: dict) -> str:
    commit = f"`{meta['commit']}`" + (" with uncommitted changes" if meta.get("dirty") else "")
    estimated = meta.get("estimated_calls", 0)
    lines = [
        f"# WOD parser evaluation, {meta['date']}",
        "",
        f"Commit {commit}, {meta['cases']} synthetic cases (`evals/wod/cases.jsonl`), "
        f"budget {meta['budget_usd']} USD, spent {meta['spent_usd']} USD"
        + (f" including the worst case of {estimated} timed-out calls" if estimated else "")
        + (", **cut short by the budget**." if meta["cut_short"] else "."),
    ]
    prices = meta.get("prices")
    if prices:
        line = f"Prices: the cost map {prices['map']}."
        for model, rate in prices.get("command_line", {}).items():
            line += (
                f" `{model}` from the command line, {rate['input_usd_per_mtok']} USD in and "
                f"{rate['output_usd_per_mtok']} USD out per million tokens."
            )
        lines += ["", line]
    lines += [
        "",
        "| Model | Effort | " + " | ".join(label for _key, label in COLUMNS) + " |",
        "|---|---|" + "---|" * len(COLUMNS),
    ]
    for result in results:
        summary = result["summary"]
        lines.append(
            f"| {result['model']} | {result['effort']} | "
            + " | ".join(_cell(key, summary.get(key), meta["cases"]) for key, _label in COLUMNS)
            + " |"
        )
    lines += [
        "",
        "Case exact leaves out the cases tagged ambiguous, which have their own rate. "
        "Recall and precision count entries paired with the expected ones by movement and set. "
        "OOV rejected is the share of out-of-vocabulary names reported as unmatched. "
        "Invented counts numbers returned where the expected value is empty.",
        "",
        "## Runs",
        "",
    ]
    for result in results:
        summary = result["summary"]
        lines.append(
            f"- {result['model']}, effort {result['effort']}: sent {_list(summary.get('efforts_sent'))}, "
            f"answered by {_list(summary.get('response_models'))}, "
            f"{summary.get('errors', 0)} errors, {summary.get('json_no_response', 0)} without a response."
        )
    for model, effort, reason in meta.get("skipped", []):
        lines.append(f"- {model}, effort {effort}: not run, {reason}.")
    lines += ["", "## Failing cases"]
    for result in results:
        failing = [case for case in result["cases"] if not case["case_exact"]]
        lines += ["", f"### {result['model']}, effort {result['effort']}: {len(failing)} of {len(result['cases'])}"]
        for case in failing:
            lines.append(f"- `{case['case_id']}` ({', '.join(case['tags'])}): " + "; ".join(case["details"][:6]))
    lines += [
        "",
        "## Known failure modes",
        "",
        "_Written from the failing cases above: what fails, how often per model, and whether a prompt change, "
        "a parser control or nothing is the right response._",
        "",
    ]
    return "\n".join(lines)


def write_report(results: list[dict], meta: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem, number = f"wod-{meta['date']}", 1
    while (out_dir / f"{stem}.md").exists():
        number += 1
        stem = f"wod-{meta['date']}-{number}"
    (out_dir / f"{stem}.json").write_text(json.dumps({"meta": meta, "results": results}, indent=2) + "\n")
    path = out_dir / f"{stem}.md"
    path.write_text(render_markdown(results, meta))
    return path


def _git(*args: str, cwd: Path = ROOT) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    except OSError, subprocess.CalledProcessError:
        return None


def commit_state(cwd: Path = ROOT) -> tuple[str, bool]:
    """The short commit and whether the tree differs from it, in which case the
    commit does not name the code that ran."""
    commit = _git("rev-parse", "--short", "HEAD", cwd=cwd)
    status = _git("status", "--porcelain", cwd=cwd)
    return (commit.strip() if commit else "unknown"), bool(status and status.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", required=True, help="comma-separated LiteLLM model strings, provider/model")
    parser.add_argument("--efforts", default="low,medium", help="comma-separated reasoning efforts")
    parser.add_argument("--budget-usd", type=float, required=True, help="stop before the next call past this cost")
    parser.add_argument("--only", default="", help="comma-separated case ids, for a quick check")
    parser.add_argument(
        "--cost-map",
        choices=("bundled", "published"),
        default="bundled",
        help="price calls by LiteLLM's bundled cost map or by the current published one",
    )
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="MODEL=IN/OUT",
        help="price a model in USD per million input/output tokens; repeatable",
    )
    parser.add_argument(
        "--allow-unpriced", action="store_true", help="run models with no price; the budget cannot stop them"
    )
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "evals")
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    if not models or not efforts:
        raise SystemExit("Give at least one model and one effort")
    for spec in args.price:
        parse_price(spec)
    configs = [(model, effort, key_for(model)) for model in models for effort in efforts]
    cases = load_cases()
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]

    with tempfile.TemporaryDirectory(prefix="virgil-wod-eval-") as scratch:
        isolate(Path(scratch))
        sys.path.insert(0, str(ROOT))
        import litellm

        check_efforts(efforts)
        prices = load_prices(args.cost_map, args.price)
        try:
            suite = asyncio.run(
                run_suite(cases, configs, args.budget_usd, Path(scratch), litellm.acompletion, args.allow_unpriced)
            )
        except UnpricedModelError as exc:
            raise SystemExit(str(exc)) from exc
    reason = refusal(suite.results)
    if reason:
        not_run = "; ".join(f"{model} {effort}: {why}" for model, effort, why in suite.skipped)
        raise SystemExit(f"No report written: {reason}." + (f" Not run: {not_run}." if not_run else ""))
    commit, dirty = commit_state()
    meta = {
        "date": date.today().isoformat(),
        "commit": commit,
        "dirty": dirty,
        "cases": len(cases),
        "budget_usd": args.budget_usd,
        "spent_usd": suite.spent,
        "estimated_calls": sum(1 for result in suite.results for run in result["runs"] if run.get("cost_estimated")),
        "cut_short": suite.cut_short,
        "configs": [[model, effort] for model, effort, _key in configs],
        "skipped": suite.skipped,
        "prices": prices,
    }
    path = write_report(suite.results, meta, args.out)
    print(f"Report: {path}" + (" (cut short by the budget)" if suite.cut_short else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
