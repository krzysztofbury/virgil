"""Live evaluation of the WOD parser (#155). It makes paid model calls.

    uv run --locked --extra dev python -m evals.wod.run \\
        --models anthropic/claude-haiku-4-5,gemini/gemini-3-flash-preview \\
        --efforts low,medium --budget-usd 2

Every case goes through the real parse_wod and call_llm, against a throwaway,
freshly migrated database whose seeded vocabulary the expected outputs are
written for. Provider keys come from the usual environment variables
(ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY...), never from an account.
The run stops before the next call once the measured cost reaches the budget,
and stops at once on a model LiteLLM has no price for, since the budget could
not stop it; --allow-unpriced runs such a model anyway, with cost unknown.
Results go to docs/evals/wod-YYYY-MM-DD.md and .json.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path

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


def load_cases(path: Path = CASES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def key_for(model: str, environ=os.environ) -> str:
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    name = KEY_VARS.get(provider)
    if not name or not environ.get(name):
        raise SystemExit(f"No key for {model}: set {name or 'the provider key variable'}")
    return environ[name]


def isolate(scratch: Path) -> None:
    """Point the app at throwaway storage before any app module is imported."""
    from cryptography.fernet import Fernet

    os.environ["VIRGIL_CENTRAL_DB_PATH"] = str(scratch / "central.db")
    os.environ["VIRGIL_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")


class UnpricedModelError(RuntimeError):
    pass


class Meter:
    """Wraps litellm.acompletion for the run: latency, cost and the raw text of
    each call, read by the harness after parse_wod returns."""

    def __init__(self, inner):
        self.inner = inner
        self.last: dict | None = None
        self.spent = 0.0

    async def __call__(self, *args, **kwargs):
        import litellm

        started = time.perf_counter()
        response = await self.inner(*args, **kwargs)
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
            "raw": choice.message.content,
            "finish_reason": str(getattr(choice, "finish_reason", "") or ""),
            "output_tokens": getattr(usage, "completion_tokens", None),
        }
        self.spent += cost or 0.0
        return response


async def run_suite(
    cases: list[dict],
    configs: list[tuple[str, str, str]],
    budget_usd: float,
    scratch: Path,
    inner,
    allow_unpriced: bool = False,
):
    """Score every case for every (model, effort, api_key). Returns the results
    and whether the budget cut the run short. Raises UnpricedModelError after
    the first call whose cost is unknown, unless allow_unpriced."""
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
    vocabulary = {m["name"] for m in movements}

    meter = Meter(inner)
    original = litellm.acompletion
    litellm.acompletion = meter
    results, cut_short = [], False
    try:
        for model, effort, api_key in configs:
            await db.execute("DELETE FROM llm_providers")
            await db.execute(
                "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 1)",
                (model.split("/", 1)[0], encrypt(api_key), model),
            )
            await set_setting(db, REASONING_EFFORT_SETTING, effort)
            await db.commit()
            scores, runs = [], []
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
                    error = f"{type(exc).__name__}: {exc}"[:300]
                if meter.last and meter.last["cost_usd"] is None and not allow_unpriced:
                    raise UnpricedModelError(
                        f"{model} has no price in LiteLLM's cost map, so the budget cannot stop it. "
                        "Pass --allow-unpriced to run it anyway."
                    )
                run = dict(meter.last or {"latency_s": None, "cost_usd": None, "raw": None, "finish_reason": ""})
                run["json_health"] = json_health(run["raw"])
                run["off_list_names"] = off_list_names(run["raw"], vocabulary)
                run["case_id"] = case["id"]
                runs.append(run)
                scores.append(score_case(case, entries, unmatched, error))
            results.append(
                {
                    "model": model,
                    "effort": effort,
                    "summary": aggregate(scores, runs),
                    "cases": [asdict(s) for s in scores],
                    "runs": runs,
                }
            )
            if cut_short:
                break
    finally:
        litellm.acompletion = original
        await db.close()
    return results, cut_short, round(meter.spent, 6)


COLUMNS = [
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


def _cell(key: str, value) -> str:
    if value is None:
        return "n/a"
    if key in PERCENT:
        return f"{value:.0%}"
    return str(value)


def render_markdown(results: list[dict], meta: dict) -> str:
    lines = [
        f"# WOD parser evaluation, {meta['date']}",
        "",
        f"Commit `{meta['commit']}`, {meta['cases']} synthetic cases (`evals/wod/cases.jsonl`), "
        f"budget {meta['budget_usd']} USD, spent {meta['spent_usd']} USD"
        + (", **cut short by the budget**." if meta["cut_short"] else "."),
        "",
        "| Model | Effort | " + " | ".join(label for _key, label in COLUMNS) + " |",
        "|---|---|" + "---|" * len(COLUMNS),
    ]
    for result in results:
        summary = result["summary"]
        lines.append(
            f"| {result['model']} | {result['effort']} | "
            + " | ".join(_cell(key, summary[key]) for key, _label in COLUMNS)
            + " |"
        )
    lines += [
        "",
        "Case exact leaves out the cases tagged ambiguous, which have their own rate. "
        "Recall and precision count entries paired with the expected ones by movement and set. "
        "OOV rejected is the share of out-of-vocabulary names reported as unmatched. "
        "Invented counts numbers returned where the expected value is empty.",
        "",
        "## Failing cases",
    ]
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


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", required=True, help="comma-separated LiteLLM model strings")
    parser.add_argument("--efforts", default="low,medium", help="comma-separated reasoning efforts")
    parser.add_argument("--budget-usd", type=float, required=True, help="stop before the next call past this cost")
    parser.add_argument("--only", default="", help="comma-separated case ids, for a quick check")
    parser.add_argument(
        "--allow-unpriced", action="store_true", help="run models LiteLLM has no price for; the budget cannot stop them"
    )
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "evals")
    args = parser.parse_args(argv)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    efforts = [e.strip() for e in args.efforts.split(",") if e.strip()]
    configs = [(model, effort, key_for(model)) for model in models for effort in efforts]
    cases = load_cases()
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]

    with tempfile.TemporaryDirectory(prefix="virgil-wod-eval-") as scratch:
        isolate(Path(scratch))
        sys.path.insert(0, str(ROOT))
        import litellm

        try:
            results, cut_short, spent = asyncio.run(
                run_suite(cases, configs, args.budget_usd, Path(scratch), litellm.acompletion, args.allow_unpriced)
            )
        except UnpricedModelError as exc:
            raise SystemExit(str(exc)) from exc
    meta = {
        "date": date.today().isoformat(),
        "commit": _commit(),
        "cases": len(cases),
        "budget_usd": args.budget_usd,
        "spent_usd": spent,
        "cut_short": cut_short,
        "configs": [[model, effort] for model, effort, _key in configs],
    }
    path = write_report(results, meta, args.out)
    print(f"Report: {path}" + (" (cut short by the budget)" if cut_short else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
