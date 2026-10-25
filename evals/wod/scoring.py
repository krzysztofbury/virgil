"""Deterministic scoring of WOD parser output against a golden case (#155).

The parser's output is structure, so it is compared field by field: no model
judges another model. Entries are paired by (movement, set_number) in order;
anything left over on either side is missed or extra.
"""

import json
from dataclasses import dataclass, field
from statistics import median

FIELDS = ("reps", "weight", "duration", "equipment")
NUMERIC = ("reps", "weight", "duration")


def _key(entry: dict) -> tuple[str, int]:
    return entry["movement"], int(entry["set_number"])


def _same(a, b) -> bool:
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < 1e-6
    return a == b


def _norm(name: str) -> str:
    return " ".join(str(name).lower().split())


def json_health(raw: str | None) -> str:
    """'valid' when the model's text parses as JSON, 'repaired' when only the
    parser's truncation repair recovers it, 'unusable' otherwise."""
    if raw is None:
        return "unusable"
    try:
        json.loads(raw)
        return "valid"
    except ValueError:
        pass
    from app.services.llm import parse_andy_response

    try:
        parse_andy_response(raw)
        return "repaired"
    except ValueError:
        return "unusable"


def off_list_names(raw: str | None, vocabulary: set[str]) -> int:
    """Entries in the model's own JSON naming a movement outside the vocabulary.

    The parser moves these to `unmatched` itself, so this counts how often that
    control had to act, not a user-visible error.
    """
    if not raw:
        return 0
    from app.services.llm import parse_andy_response

    try:
        payload = parse_andy_response(raw)
    except ValueError:
        return 0
    entries = payload.get("entries") if isinstance(payload, dict) else None
    known = {_norm(name) for name in vocabulary}
    items = entries if isinstance(entries, list) else []
    return sum(1 for item in items if isinstance(item, dict) and _norm(item.get("movement") or "") not in known)


@dataclass
class CaseScore:
    case_id: str
    tags: list[str]
    expected_entries: int
    returned_entries: int
    paired: int
    field_hits: dict[str, int]
    exact_entries: int
    invented_values: int
    oov_expected: int
    oov_rejected: int
    oov_hallucinated: int
    oov_dropped: int
    unexpected_unmatched: list[str]
    case_exact: bool
    error: str | None = None
    details: list[str] = field(default_factory=list)


def score_case(case: dict, entries: list[dict], unmatched: list[str], error: str | None = None) -> CaseScore:
    """Compare one parse with its expected output. `entries` are dicts with the
    expected keys; a parse that raised passes no entries and its error."""
    expected = case["expected"]["entries"]
    expected_unmatched = case["expected"]["unmatched"]

    pending: dict[tuple[str, int], list[dict]] = {}
    for entry in entries:
        pending.setdefault(_key(entry), []).append(entry)
    pairs, missed = [], []
    for want in expected:
        bucket = pending.get(_key(want))
        if bucket:
            pairs.append((want, bucket.pop(0)))
        else:
            missed.append(want)
    extra = [entry for bucket in pending.values() for entry in bucket]

    field_hits = {name: sum(1 for want, got in pairs if _same(want[name], got.get(name))) for name in FIELDS}
    exact = sum(1 for want, got in pairs if all(_same(want[name], got.get(name)) for name in FIELDS))
    invented = sum(
        1 for want, got in pairs for name in NUMERIC if want[name] is None and got.get(name) is not None
    )

    returned_unmatched = {_norm(name) for name in unmatched}
    rejected = [name for name in expected_unmatched if _norm(name) in returned_unmatched]
    not_rejected = [name for name in expected_unmatched if _norm(name) not in returned_unmatched]
    # An out-of-vocabulary name that is not reported either became an entry it
    # should not have (hallucinated) or vanished (dropped). Extra entries are the
    # only trace of the first.
    hallucinated = len(not_rejected) if extra else 0
    expected_norm = {_norm(name) for name in expected_unmatched}
    unexpected = sorted(name for name in unmatched if _norm(name) not in expected_norm)

    details = [f"missed {m['movement']} set {m['set_number']}" for m in missed]
    details += [f"extra {x['movement']} set {x['set_number']}" for x in extra]
    for want, got in pairs:
        wrong = [name for name in FIELDS if not _same(want[name], got.get(name))]
        if wrong:
            shown = ", ".join(f"{name} {got.get(name)!r} != {want[name]!r}" for name in wrong)
            details.append(f"{want['movement']} set {want['set_number']}: {shown}")
    details += [f"not reported as unmatched: {name}" for name in not_rejected]
    details += [f"unexpected unmatched: {name}" for name in unexpected]
    if error:
        details.insert(0, f"error: {error}")

    return CaseScore(
        case_id=case["id"],
        tags=list(case.get("tags", [])),
        expected_entries=len(expected),
        returned_entries=len(entries),
        paired=len(pairs),
        field_hits=field_hits,
        exact_entries=exact,
        invented_values=invented,
        oov_expected=len(expected_unmatched),
        oov_rejected=len(rejected),
        oov_hallucinated=hallucinated,
        oov_dropped=len(not_rejected) - hallucinated,
        unexpected_unmatched=unexpected,
        case_exact=error is None and not missed and not extra and exact == len(pairs) and not not_rejected
        and not unexpected,
        error=error,
        details=details,
    )


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _percentile(values: list[float], share: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def aggregate(scores: list[CaseScore], runs: list[dict]) -> dict:
    """One row of the comparison table. `runs` holds per-call measurements:
    latency_s, cost_usd (None when the provider has no price) and json_health."""
    expected = sum(s.expected_entries for s in scores)
    returned = sum(s.returned_entries for s in scores)
    paired = sum(s.paired for s in scores)
    costs = [r["cost_usd"] for r in runs if r.get("cost_usd") is not None]
    latencies = [r["latency_s"] for r in runs if r.get("latency_s") is not None]
    health = [r.get("json_health") for r in runs]
    return {
        "cases": len(scores),
        "case_exact": _ratio(sum(s.case_exact for s in scores), len(scores)),
        "entry_exact": _ratio(sum(s.exact_entries for s in scores), expected),
        "recall": _ratio(paired, expected),
        "precision": _ratio(paired, returned),
        **{f"{name}_accuracy": _ratio(sum(s.field_hits[name] for s in scores), paired) for name in FIELDS},
        "invented_values": sum(s.invented_values for s in scores),
        "oov_rejected": _ratio(sum(s.oov_rejected for s in scores), sum(s.oov_expected for s in scores)),
        "oov_hallucinated": sum(s.oov_hallucinated for s in scores),
        "oov_dropped": sum(s.oov_dropped for s in scores),
        "errors": sum(1 for s in scores if s.error),
        "json_valid": health.count("valid"),
        "json_repaired": health.count("repaired"),
        "json_unusable": health.count("unusable"),
        "off_list_names": sum(r.get("off_list_names", 0) for r in runs),
        "latency_p50_s": round(median(latencies), 2) if latencies else None,
        "latency_p95_s": _percentile(latencies, 0.95),
        "cost_per_100_usd": round(sum(costs) / len(costs) * 100, 4) if costs else None,
        "cost_known_for": f"{len(costs)}/{len(runs)}",
    }
