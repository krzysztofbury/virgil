"""Finance: the user's fortress of layers, their accounts and monthly balances.

Amounts are stored as integers in minor units (grosze, cents) of one base
currency and returned as decimals. A balance holds from its date until the
account's next one, so a month where one account was not updated still has a
total, and that account is marked stale. An archived account counts only up
to its last balance.

Unknown is not zero: a score needs an essential monthly cost, at least one
income source and a status for every scored policy, and says which is missing.
"""

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import aiosqlite

from app.db import get_setting, set_setting

METRICS = ("cushion_months", "independent_income_share", "policies")
ACCOUNT_KINDS = ("cash", "bonds", "retirement", "brokerage", "crypto", "property", "other")
OWNERSHIP = ("own", "joint", "excluded")
POLICY_STATUSES = ("present", "absent", "not_applicable")
BASE_CURRENCY_SETTING = "finance_base_currency"
_MAX_MINOR = 10**15


class FinanceError(ValueError):
    """A request the finance data cannot take. The message names the field."""


class FinanceNotFound(LookupError):
    """An account or layer that does not exist."""


def to_minor(value: object, field: str = "amount") -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise FinanceError(f"{field} must be a number")
    try:
        number = Decimal(str(value).strip().replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise FinanceError(f"{field} must be a number") from exc
    if not number.is_finite():
        raise FinanceError(f"{field} must be a number")
    minor = int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if abs(minor) > _MAX_MINOR:
        raise FinanceError(f"{field} is too large")
    return minor


def from_minor(value: int | None) -> float | None:
    return None if value is None else round(value / 100, 2)


def currency_code(value: object, field: str = "currency") -> str:
    code = str(value or "").strip().upper()
    if code and (len(code) != 3 or not code.isalpha()):
        raise FinanceError(f"{field} must be a three-letter code such as PLN or EUR")
    return code


def iso_day(value: object, field: str = "date") -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise FinanceError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc


def _text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise FinanceError(f"{field} is required")
    if len(text) > limit:
        raise FinanceError(f"{field} is longer than {limit} characters")
    return text


# --- Reading ---------------------------------------------------------------


async def base_currency(db: aiosqlite.Connection) -> str:
    return await get_setting(db, BASE_CURRENCY_SETTING, "PLN") or "PLN"


async def list_layers(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT l.id, l.name, l.description, l.counts_toward_cushion, l.display_order,
                  r.metric, r.max_points, r.thresholds
           FROM finance_layers l LEFT JOIN finance_score_rules r ON r.layer_id = l.id
           ORDER BY l.display_order, l.id"""
    )
    layers = []
    for row in rows:
        rule = None
        if row["metric"]:
            rule = {
                "metric": row["metric"],
                "max_points": row["max_points"],
                "thresholds": json.loads(row["thresholds"]),
            }
        layers.append(
            {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "counts_toward_cushion": bool(row["counts_toward_cushion"]),
                "display_order": row["display_order"],
                "rule": rule,
            }
        )
    return layers


async def list_accounts(db: aiosqlite.Connection, *, include_archived: bool = True) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT a.*, l.name AS layer FROM finance_accounts a JOIN finance_layers l ON l.id = a.layer_id
           ORDER BY l.display_order, a.display_order, a.name"""
    )
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "institution": row["institution"],
            "layer_id": row["layer_id"],
            "layer": row["layer"],
            "kind": row["kind"],
            "currency": row["currency"],
            "ownership": row["ownership"],
            "archived": bool(row["archived"]),
            "display_order": row["display_order"],
        }
        for row in rows
        if include_archived or not row["archived"]
    ]


async def _essential_cost(db: aiosqlite.Connection, day: str) -> tuple[int | None, str | None]:
    rows = await db.execute_fetchall(
        "SELECT essential_monthly_cost_minor, effective_from FROM finance_assumptions "
        "WHERE effective_from <= ? ORDER BY effective_from DESC LIMIT 1",
        (day,),
    )
    return (rows[0][0], rows[0][1]) if rows else (None, None)


async def _income(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall("SELECT * FROM finance_income_sources ORDER BY name")
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "monthly_amount": from_minor(row["monthly_amount_minor"]),
            "monthly_amount_minor": row["monthly_amount_minor"],
            "independent_of_work": bool(row["independent_of_work"]),
            "confirmed_on": row["confirmed_on"],
            "note": row["note"],
        }
        for row in rows
    ]


async def _policies(db: aiosqlite.Connection) -> dict[str, dict]:
    rows = await db.execute_fetchall("SELECT * FROM finance_policies ORDER BY type")
    return {
        row["type"]: {
            "type": row["type"],
            "status": row["status"],
            "confirmed_on": row["confirmed_on"],
            "note": row["note"],
        }
        for row in rows
    }


def _threshold_points(thresholds: list, value: float, max_points: int) -> int:
    for minimum, points in sorted(thresholds, key=lambda pair: pair[0], reverse=True):
        if value >= minimum:
            return min(int(points), max_points)
    return 0


def _score_layer(rule: dict, cushion_months: float | None, independent_share: float | None, policies: dict) -> dict:
    metric, max_points, thresholds = rule["metric"], rule["max_points"], rule["thresholds"]
    if metric == "cushion_months":
        if cushion_months is None:
            return {"value": None, "points": None, "missing": "an essential monthly cost"}
        return {"value": round(cushion_months, 2), "points": _threshold_points(thresholds, cushion_months, max_points)}
    if metric == "independent_income_share":
        if independent_share is None:
            return {"value": None, "points": None, "missing": "an essential monthly cost and income sources"}
        return {
            "value": round(independent_share, 1),
            "points": _threshold_points(thresholds, independent_share, max_points),
        }
    missing = sorted(kind for kind in thresholds if kind not in policies)
    if missing:
        return {"value": None, "points": None, "missing": "a status for " + ", ".join(missing)}
    applicable = {kind: points for kind, points in thresholds.items() if policies[kind]["status"] != "not_applicable"}
    earned = sum(points for kind, points in applicable.items() if policies[kind]["status"] == "present")
    possible = sum(applicable.values())
    points = max_points if possible == 0 else round(earned / possible * max_points)
    return {"value": {kind: policies[kind]["status"] for kind in thresholds}, "points": min(points, max_points)}


async def _balances_as_of(db: aiosqlite.Connection, day: str) -> dict[int, dict]:
    """Each account's latest balance on or before `day`."""
    rows = await db.execute_fetchall(
        """SELECT b.* FROM finance_balances b
           JOIN (SELECT account_id, MAX(date) AS date FROM finance_balances WHERE date <= ? GROUP BY account_id) latest
             ON latest.account_id = b.account_id AND latest.date = b.date""",
        (day,),
    )
    return {row["account_id"]: dict(row) for row in rows}


async def _last_balance_dates(db: aiosqlite.Connection) -> dict[int, str]:
    rows = await db.execute_fetchall("SELECT account_id, MAX(date) FROM finance_balances GROUP BY account_id")
    return {row[0]: row[1] for row in rows}


async def _state(db: aiosqlite.Connection, day: str, layers: list[dict], accounts: list[dict]) -> dict:
    balances = await _balances_as_of(db, day)
    last_dates = await _last_balance_dates(db)
    snapshot = max((balance["date"] for balance in balances.values()), default=None)
    layer_by_id = {layer["id"]: layer for layer in layers}
    totals = {layer["id"]: 0 for layer in layers}
    rows, cushion, total, excluded = [], 0, 0, 0
    for account in accounts:
        balance = balances.get(account["id"])
        # A closed account's last balance holds for the snapshot it was recorded
        # in, not for every month after it.
        if account["archived"] and (
            balance is None or (balance["date"] == last_dates.get(account["id"]) and balance["date"] != snapshot)
        ):
            continue
        amount = balance["amount_minor"] if balance else None
        if amount is not None:
            if account["ownership"] == "excluded":
                excluded += amount
            else:
                totals[account["layer_id"]] += amount
                total += amount
                if layer_by_id[account["layer_id"]]["counts_toward_cushion"]:
                    cushion += amount
        rows.append(
            {
                **account,
                "amount": from_minor(amount),
                "native_amount": from_minor(balance["native_amount_minor"]) if balance else None,
                "native_currency": balance["native_currency"] if balance else "",
                "as_of": balance["date"] if balance else None,
                "stale": balance is None or balance["date"] != snapshot,
            }
        )
    return {
        "snapshot": snapshot,
        "accounts": rows,
        "layer_totals": totals,
        "cushion": cushion,
        "total": total,
        "excluded": excluded,
    }


async def overview(db: aiosqlite.Connection, day: str | None = None) -> dict:
    """Where the fortress stands on `day` (default today)."""
    day = iso_day(day) if day else date.today().isoformat()
    layers = await list_layers(db)
    accounts = await list_accounts(db)
    state = await _state(db, day, layers, accounts)
    cost, cost_from = await _essential_cost(db, day)
    income = await _income(db)
    policies = await _policies(db)

    cushion_months = state["cushion"] / cost if cost else None
    independent = sum(source["monthly_amount_minor"] for source in income if source["independent_of_work"])
    stable = sum(source["monthly_amount_minor"] for source in income)
    independent_share = independent / cost * 100 if cost and income else None
    stable_share = stable / cost * 100 if cost and income else None

    # Two readings of the same rules: income that does not depend on current work
    # only, and all stable income (a partner's salary, say) counted as well.
    scored, total_points, all_income_points, max_total, known = [], 0, 0, 0, True
    for layer in layers:
        result = None
        if layer["rule"]:
            result = _score_layer(layer["rule"], cushion_months, independent_share, policies)
            max_total += layer["rule"]["max_points"]
            if result["points"] is None:
                known = False
            else:
                total_points += result["points"]
                with_all = result["points"]
                if layer["rule"]["metric"] == "independent_income_share":
                    with_all = _score_layer(layer["rule"], cushion_months, stable_share, policies)["points"]
                    result["points_with_all_income"] = with_all
                all_income_points += with_all
        scored.append(
            {
                "id": layer["id"],
                "name": layer["name"],
                "description": layer["description"],
                "counts_toward_cushion": layer["counts_toward_cushion"],
                "total": from_minor(state["layer_totals"][layer["id"]]),
                "accounts": [account for account in state["accounts"] if account["layer_id"] == layer["id"]],
                "score": None if result is None else {**result, "max_points": layer["rule"]["max_points"]},
            }
        )
    for layer in scored:
        for account in layer["accounts"]:
            for key in ("layer_id", "display_order"):
                account.pop(key, None)

    return {
        "date": day,
        "currency": await base_currency(db),
        "latest_snapshot": state["snapshot"],
        "layers": scored,
        "total": from_minor(state["total"]),
        "excluded_total": from_minor(state["excluded"]),
        "cushion": from_minor(state["cushion"]),
        "essential_monthly_cost": from_minor(cost),
        "essential_cost_since": cost_from,
        "cushion_months": None if cushion_months is None else round(cushion_months, 2),
        "income_sources": [{k: v for k, v in source.items() if k != "monthly_amount_minor"} for source in income],
        "independent_income_share": None if independent_share is None else round(independent_share, 1),
        "stable_income_share": None if stable_share is None else round(stable_share, 1),
        "policies": list(policies.values()),
        "score": {
            "points": total_points if known else None,
            "points_with_all_income": all_income_points if known else None,
            "known_points": total_points,
            "max_points": max_total,
        },
        "stale_accounts": [account["name"] for account in state["accounts"] if account["stale"]],
    }


async def history(db: aiosqlite.Connection, months: int = 12) -> dict:
    """Totals and cushion months at every date balances were recorded."""
    months = max(1, min(int(months), 240))
    since = (date.today() - timedelta(days=round(months * 30.44))).isoformat()
    days = [
        row[0]
        for row in await db.execute_fetchall(
            "SELECT DISTINCT date FROM finance_balances WHERE date >= ? ORDER BY date", (since,)
        )
    ]
    layers = await list_layers(db)
    accounts = await list_accounts(db)
    points = []
    for day in days:
        state = await _state(db, day, layers, accounts)
        cost, _ = await _essential_cost(db, day)
        points.append(
            {
                "date": day,
                "total": from_minor(state["total"]),
                "cushion": from_minor(state["cushion"]),
                "essential_monthly_cost": from_minor(cost),
                "cushion_months": round(state["cushion"] / cost, 2) if cost else None,
                "layers": {layer["name"]: from_minor(state["layer_totals"][layer["id"]]) for layer in layers},
                "stale_accounts": sum(1 for account in state["accounts"] if account["stale"]),
            }
        )
    return {"months": months, "since": since, "currency": await base_currency(db), "points": points}


# --- Writing ---------------------------------------------------------------


async def _find(db: aiosqlite.Connection, table: str, ref: object, what: str) -> dict:
    if isinstance(ref, bool) or ref is None or ref == "":
        raise FinanceError(f"{what} is required")
    # A JSON number is an id; text is always a name, even "2024".
    if isinstance(ref, int):
        rows = await db.execute_fetchall(f"SELECT * FROM {table} WHERE id = ?", (int(ref),))  # noqa: S608 - fixed table
    else:
        rows = await db.execute_fetchall(f"SELECT * FROM {table} WHERE name = ?", (str(ref).strip(),))  # noqa: S608
    if not rows:
        raise FinanceNotFound(f"No {what} {ref!r}")
    return dict(rows[0])


async def _write(db: aiosqlite.Connection, statements) -> None:
    """Run every write or none."""
    try:
        for sql, params in statements:
            await db.execute(sql, params)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def record_balances(db: aiosqlite.Connection, day: object, entries: list, *, source: str = "api") -> dict:
    """Balances of several accounts on one date, all or none. A null amount removes that date's balance."""
    day = iso_day(day)
    if not isinstance(entries, list) or not entries:
        raise FinanceError("entries must list at least one account")
    if len(entries) > 200:
        raise FinanceError("entries can list at most 200 accounts")
    base = await base_currency(db)
    statements, seen, recorded, removed = [], set(), 0, 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise FinanceError(f"entries[{index}] must be an object")
        account = await _find(db, "finance_accounts", entry.get("account"), "account")
        if account["id"] in seen:
            raise FinanceError(f"{account['name']} is listed twice")
        seen.add(account["id"])
        if entry.get("amount") is None:
            statements.append(("DELETE FROM finance_balances WHERE account_id = ? AND date = ?", (account["id"], day)))
            removed += 1
            continue
        amount = to_minor(entry["amount"], f"entries[{index}].amount")
        native_currency = currency_code(entry.get("native_currency") or account["currency"], "native_currency")
        native = entry.get("native_amount")
        native_minor = to_minor(native, f"entries[{index}].native_amount") if native is not None else None
        if native_minor is None or native_currency == base:
            native_minor, native_currency = None, ""
        note = _text(entry.get("note"), "note", 300)
        statements.append(
            (
                """INSERT INTO finance_balances (account_id, date, amount_minor, native_amount_minor, native_currency, source, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_id, date) DO UPDATE SET amount_minor = excluded.amount_minor,
                     native_amount_minor = excluded.native_amount_minor, native_currency = excluded.native_currency,
                     source = excluded.source, note = excluded.note, updated_at = datetime('now')""",
                (account["id"], day, amount, native_minor, native_currency, source, note),
            )
        )
        recorded += 1
    await _write(db, statements)
    return {"date": day, "recorded": recorded, "removed": removed}


async def upsert_account(db: aiosqlite.Connection, data: dict) -> dict:
    """Create an account, or change one named by `id` (or by `name` when no id is given)."""
    existing = None
    if data.get("id") is not None:
        existing = await _find(db, "finance_accounts", data["id"], "account")
    elif data.get("name"):
        try:
            existing = await _find(db, "finance_accounts", str(data["name"]), "account")
        except FinanceNotFound:
            existing = None
    current = existing or {"institution": "", "kind": "cash", "currency": "", "ownership": "own", "archived": 0}
    name = _text(data.get("name", current.get("name")), "name", 80, required=True)
    if "layer" in data or existing is None:
        layer_id = (await _find(db, "finance_layers", data.get("layer"), "layer"))["id"]
    else:
        layer_id = existing["layer_id"]
    kind = data.get("kind", current["kind"])
    if kind not in ACCOUNT_KINDS:
        raise FinanceError(f"kind must be one of {', '.join(ACCOUNT_KINDS)}")
    ownership = data.get("ownership", current["ownership"])
    if ownership not in OWNERSHIP:
        raise FinanceError(f"ownership must be one of {', '.join(OWNERSHIP)}")
    values = (
        name,
        _text(data.get("institution", current["institution"]), "institution", 80),
        layer_id,
        kind,
        currency_code(data.get("currency", current["currency"])),
        ownership,
        1 if data.get("archived", bool(current["archived"])) else 0,
    )
    try:
        if existing:
            await _write(
                db,
                [
                    (
                        "UPDATE finance_accounts SET name = ?, institution = ?, layer_id = ?, kind = ?, currency = ?, "
                        "ownership = ?, archived = ? WHERE id = ?",
                        (*values, existing["id"]),
                    )
                ],
            )
            account_id = existing["id"]
        else:
            cursor = await db.execute(
                "INSERT INTO finance_accounts (name, institution, layer_id, kind, currency, ownership, archived) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            await db.commit()
            account_id = cursor.lastrowid
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        raise FinanceError(f"Another account is already called {name!r}") from exc
    return next(account for account in await list_accounts(db) if account["id"] == account_id)


async def set_assumptions(db: aiosqlite.Connection, data: dict) -> dict:
    """Essential monthly cost from a date, the income sources and the policies.

    `income_sources` replaces the whole list; `policies` sets the types it names.
    """
    statements = []
    if data.get("essential_monthly_cost") is not None:
        cost = to_minor(data["essential_monthly_cost"], "essential_monthly_cost")
        if cost <= 0:
            raise FinanceError("essential_monthly_cost must be above zero")
        since = iso_day(data.get("effective_from") or date.today().isoformat(), "effective_from")
        statements.append(
            (
                "INSERT INTO finance_assumptions (effective_from, essential_monthly_cost_minor) VALUES (?, ?) "
                "ON CONFLICT(effective_from) DO UPDATE SET essential_monthly_cost_minor = excluded.essential_monthly_cost_minor",
                (since, cost),
            )
        )
    if data.get("income_sources") is not None:
        sources = data["income_sources"]
        if not isinstance(sources, list) or len(sources) > 50:
            raise FinanceError("income_sources must be a list of at most 50 sources")
        statements.append(("DELETE FROM finance_income_sources", ()))
        names = set()
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise FinanceError(f"income_sources[{index}] must be an object")
            name = _text(source.get("name"), f"income_sources[{index}].name", 80, required=True)
            if name.lower() in names:
                raise FinanceError(f"{name} is listed twice")
            names.add(name.lower())
            amount = to_minor(source.get("monthly_amount"), f"income_sources[{index}].monthly_amount")
            if amount < 0:
                raise FinanceError(f"income_sources[{index}].monthly_amount cannot be negative")
            confirmed = source.get("confirmed_on") or ""
            statements.append(
                (
                    "INSERT INTO finance_income_sources (name, monthly_amount_minor, independent_of_work, confirmed_on, note) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        name,
                        amount,
                        1 if source.get("independent_of_work") else 0,
                        iso_day(confirmed, "confirmed_on") if confirmed else "",
                        _text(source.get("note"), "note", 300),
                    ),
                )
            )
    if data.get("policies") is not None:
        policies = data["policies"]
        if not isinstance(policies, list) or len(policies) > 30:
            raise FinanceError("policies must be a list of at most 30 policies")
        for index, policy in enumerate(policies):
            if not isinstance(policy, dict):
                raise FinanceError(f"policies[{index}] must be an object")
            kind = _text(policy.get("type"), f"policies[{index}].type", 40, required=True).lower()
            status = policy.get("status")
            if status is None:
                statements.append(("DELETE FROM finance_policies WHERE type = ?", (kind,)))
                continue
            if status not in POLICY_STATUSES:
                raise FinanceError(f"policies[{index}].status must be one of {', '.join(POLICY_STATUSES)}")
            confirmed = policy.get("confirmed_on") or ""
            statements.append(
                (
                    "INSERT INTO finance_policies (type, status, confirmed_on, note) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(type) DO UPDATE SET status = excluded.status, confirmed_on = excluded.confirmed_on, "
                    "note = excluded.note",
                    (
                        kind,
                        status,
                        iso_day(confirmed, "confirmed_on") if confirmed else "",
                        _text(policy.get("note"), "note", 300),
                    ),
                )
            )
    currency = data.get("base_currency")
    if currency is not None:
        code = currency_code(currency, "base_currency")
        if not code:
            raise FinanceError("base_currency must be a three-letter code such as PLN or EUR")
    if not statements and currency is None:
        raise FinanceError("Nothing to set: give essential_monthly_cost, income_sources, policies or base_currency")
    await _write(db, statements)
    if currency is not None:
        await set_setting(db, BASE_CURRENCY_SETTING, code)
    return await assumptions(db)


async def assumptions(db: aiosqlite.Connection) -> dict:
    costs = await db.execute_fetchall(
        "SELECT effective_from, essential_monthly_cost_minor FROM finance_assumptions ORDER BY effective_from DESC"
    )
    return {
        "base_currency": await base_currency(db),
        "essential_monthly_cost": [{"effective_from": row[0], "amount": from_minor(row[1])} for row in costs],
        "income_sources": [
            {k: v for k, v in source.items() if k != "monthly_amount_minor"} for source in await _income(db)
        ],
        "policies": list((await _policies(db)).values()),
    }


def _validate_rule(rule: dict, layer: str) -> tuple[str, int, str]:
    if not isinstance(rule, dict):
        raise FinanceError(f"{layer}: rule must be an object or null")
    metric = rule.get("metric")
    if metric not in METRICS:
        raise FinanceError(f"{layer}: metric must be one of {', '.join(METRICS)}")
    max_points = rule.get("max_points")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or not 0 <= max_points <= 100:
        raise FinanceError(f"{layer}: max_points must be a whole number from 0 to 100")
    thresholds = rule.get("thresholds")
    if metric == "policies":
        if not isinstance(thresholds, dict) or not 1 <= len(thresholds) <= 12:
            raise FinanceError(f"{layer}: thresholds must map 1 to 12 policy types to points")
        cleaned = {}
        for kind, points in thresholds.items():
            key = _text(kind, f"{layer}: policy type", 40, required=True).lower()
            if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= max_points:
                raise FinanceError(f"{layer}: points for {key} must be a whole number from 0 to {max_points}")
            cleaned[key] = points
        return metric, max_points, json.dumps(cleaned)
    if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 10:
        raise FinanceError(f"{layer}: thresholds must list 1 to 10 [minimum, points] pairs")
    cleaned = []
    for pair in thresholds:
        if not isinstance(pair, list | tuple) or len(pair) != 2:
            raise FinanceError(f"{layer}: each threshold is a [minimum, points] pair")
        minimum, points = pair
        if isinstance(minimum, bool) or not isinstance(minimum, int | float) or not 0 <= minimum <= 10_000:
            raise FinanceError(f"{layer}: a threshold minimum must be a number from 0 to 10000")
        if isinstance(points, bool) or not isinstance(points, int) or not 0 <= points <= max_points:
            raise FinanceError(f"{layer}: threshold points must be a whole number from 0 to {max_points}")
        cleaned.append([minimum, points])
    return metric, max_points, json.dumps(sorted(cleaned, key=lambda pair: pair[0], reverse=True))


async def scoring(db: aiosqlite.Connection) -> dict:
    layers = await list_layers(db)
    return {
        "layers": layers,
        "max_points": sum(layer["rule"]["max_points"] for layer in layers if layer["rule"]),
        "metrics": list(METRICS),
    }


async def set_scoring(db: aiosqlite.Connection, layers: list | None = None, remove: list | None = None) -> dict:
    """Create or change layers and their score rules; remove layers no account uses.

    A layer is matched by `id`, else by `name`; `rename` gives it a new name.
    `rule: null` stops scoring it; leaving `rule` out keeps the current one.
    """
    if not layers and not remove:
        raise FinanceError("Give layers to set or names to remove")
    statements, new_layers = [], []
    for name in remove or []:
        layer = await _find(db, "finance_layers", name, "layer")
        used = await db.execute_fetchall("SELECT COUNT(*) FROM finance_accounts WHERE layer_id = ?", (layer["id"],))
        if used[0][0]:
            raise FinanceError(f"{layer['name']} still has accounts; move them first")
        statements.append(("DELETE FROM finance_layers WHERE id = ?", (layer["id"],)))
    for index, item in enumerate(layers or []):
        if not isinstance(item, dict):
            raise FinanceError(f"layers[{index}] must be an object")
        ref = item.get("id") if item.get("id") is not None else item.get("name")
        try:
            existing = await _find(db, "finance_layers", ref, "layer")
        except FinanceNotFound:
            existing = None
        label = str(item.get("name") or ref)
        name = _text(item.get("rename") or item.get("name") or (existing or {}).get("name"), "name", 60, required=True)
        description = _text(item.get("description", (existing or {}).get("description", "")), "description", 300)
        cushion = item.get("counts_toward_cushion", bool((existing or {}).get("counts_toward_cushion", 0)))
        order = item.get("display_order", (existing or {}).get("display_order", 100 + index))
        if isinstance(order, bool) or not isinstance(order, int):
            raise FinanceError(f"{label}: display_order must be a whole number")
        rule = _validate_rule(item["rule"], label) if item.get("rule") is not None else None
        if existing:
            statements.append(
                (
                    "UPDATE finance_layers SET name = ?, description = ?, counts_toward_cushion = ?, display_order = ? "
                    "WHERE id = ?",
                    (name, description, 1 if cushion else 0, order, existing["id"]),
                )
            )
            if "rule" in item:
                statements.append(("DELETE FROM finance_score_rules WHERE layer_id = ?", (existing["id"],)))
                if rule:
                    statements.append(
                        (
                            "INSERT INTO finance_score_rules (layer_id, metric, max_points, thresholds) VALUES (?, ?, ?, ?)",
                            (existing["id"], *rule),
                        )
                    )
        else:
            new_layers.append((name, description, 1 if cushion else 0, order, rule))
    try:
        for sql, params in statements:
            await db.execute(sql, params)
        for name, description, cushion, order, rule in new_layers:
            cursor = await db.execute(
                "INSERT INTO finance_layers (name, description, counts_toward_cushion, display_order) VALUES (?, ?, ?, ?)",
                (name, description, cushion, order),
            )
            if rule:
                await db.execute(
                    "INSERT INTO finance_score_rules (layer_id, metric, max_points, thresholds) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, *rule),
                )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        await db.rollback()
        raise FinanceError("Two layers cannot share a name") from exc
    except BaseException:
        await db.rollback()
        raise
    return await scoring(db)
