"""Finance pages: where the fortress stands, a month's balances, and its setup.

Every number comes from app/services/finance.py, the same code the API and MCP
use, so the page and an agent never disagree.
"""

import logging
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.feedback import error_redirect, success_redirect
from app.i18n import N_, _, localized, ngettext
from app.main import templates
from app.services import finance
from app.services.finance import FinanceError, FinanceNotFound
from app.user_db import get_user_db_from_request

logger = logging.getLogger(__name__)


async def require_finance(request: Request):
    if not getattr(request.state, "features", {}).get("finance", False):
        raise HTTPException(status_code=303, headers={"Location": "/"})


router = APIRouter(dependencies=[Depends(require_finance)])

SETUP = "/finance/setup"


def _parse_thresholds(metric: str, text: str) -> list | dict:
    """'12:40, 6:30' for months or percent; 'life:10, health:8' for policies."""
    pairs = [part.strip() for part in text.replace(";", ",").replace("\n", ",").split(",") if part.strip()]
    parsed = []
    for pair in pairs:
        key, sep, points = pair.partition(":")
        if not sep:
            raise FinanceError(N_("Write each threshold as minimum:points, not {pair!r}"), pair=pair)
        try:
            value = int(points.strip())
        except ValueError as exc:
            raise FinanceError(N_("Points must be whole numbers, not {value!r}"), value=points.strip()) from exc
        if metric == "policies":
            parsed.append((key.strip(), value))
        else:
            try:
                minimum = float(key.strip().replace(",", "."))
            except ValueError as exc:
                raise FinanceError(
                    N_("A threshold minimum must be a number, not {value!r}"), value=key.strip()
                ) from exc
            parsed.append([int(minimum) if minimum.is_integer() else minimum, value])
    return dict(parsed) if metric == "policies" else parsed


def _thresholds_text(rule: dict | None) -> str:
    if not rule:
        return ""
    if rule["metric"] == "policies":
        return ", ".join(f"{kind}:{points}" for kind, points in rule["thresholds"].items())
    return ", ".join(f"{minimum:g}:{points}" for minimum, points in rule["thresholds"])


@router.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request):
    db = get_user_db_from_request(request)
    overview = await finance.overview(db)
    history = await finance.history(db, 36)
    accounts = [account for account in await finance.list_accounts(db, include_archived=False)]
    latest = {account["id"]: account for layer in overview["layers"] for account in layer["accounts"]}
    return templates.TemplateResponse(
        request,
        "finance.html",
        {
            "overview": overview,
            "history": history["points"],
            "accounts": [{**account, "latest": latest.get(account["id"], {}).get("amount")} for account in accounts],
            "today": date.today().isoformat(),
        },
    )


@router.post("/finance/balances")
async def save_balances(request: Request):
    """The monthly form: every filled amount for one date, all or none. A blank field is skipped."""
    form = await request.form()
    db = get_user_db_from_request(request)
    entries = []
    for key, value in form.multi_items():
        if key.startswith("balance_") and str(value).strip():
            entries.append({"account": int(key.removeprefix("balance_")), "amount": str(value)})
    if not entries:
        return error_redirect(request, "/finance#record", _("Fill in at least one balance."))
    try:
        result = await finance.record_balances(db, form.get("date", ""), entries, source="web")
    except (FinanceError, FinanceNotFound) as exc:
        return error_redirect(request, "/finance#record", localized(exc))
    return success_redirect(
        request,
        "/finance",
        ngettext(
            "Recorded {num} balance for {date}.", "Recorded {num} balances for {date}.", result["recorded"]
        ).format(num=result["recorded"], date=result["date"]),
    )


@router.get("/finance/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    db = get_user_db_from_request(request)
    layers = await finance.list_layers(db)
    policy_types = []
    for layer in layers:
        if layer["rule"] and layer["rule"]["metric"] == "policies":
            policy_types += [kind for kind in layer["rule"]["thresholds"] if kind not in policy_types]
    assumptions = await finance.assumptions(db)
    statuses = {policy["type"]: policy for policy in assumptions["policies"]}
    policy_types += [kind for kind in statuses if kind not in policy_types]
    return templates.TemplateResponse(
        request,
        "finance_setup.html",
        {
            "layers": [{**layer, "thresholds_text": _thresholds_text(layer["rule"])} for layer in layers],
            "accounts": await finance.list_accounts(db),
            "assumptions": assumptions,
            "policy_types": policy_types,
            "policy_statuses": statuses,
            "kinds": finance.ACCOUNT_KINDS,
            "ownership": finance.OWNERSHIP,
            "metrics": finance.METRICS,
            "today": date.today().isoformat(),
        },
    )


@router.post("/finance/accounts")
async def save_account(
    request: Request,
    account_id: str = Form(""),
    name: str = Form(...),
    layer_id: int = Form(...),
    kind: str = Form("cash"),
    ownership: str = Form("own"),
    institution: str = Form(""),
    currency: str = Form(""),
    archived: str = Form(""),
):
    db = get_user_db_from_request(request)
    data = {
        "name": name,
        "layer": layer_id,
        "kind": kind,
        "ownership": ownership,
        "institution": institution,
        "currency": currency,
        "archived": bool(archived),
    }
    if account_id.strip():
        data["id"] = int(account_id)
    try:
        account = await finance.upsert_account(db, data)
    except (FinanceError, FinanceNotFound) as exc:
        return error_redirect(request, f"{SETUP}#accounts", localized(exc))
    return success_redirect(request, f"{SETUP}#accounts", _("{name} saved.").format(name=account["name"]))


@router.post("/finance/cost")
async def save_cost(request: Request, essential_monthly_cost: str = Form(...), effective_from: str = Form("")):
    db = get_user_db_from_request(request)
    try:
        await finance.set_assumptions(
            db, {"essential_monthly_cost": essential_monthly_cost, "effective_from": effective_from or None}
        )
    except FinanceError as exc:
        return error_redirect(request, f"{SETUP}#cost", localized(exc))
    return success_redirect(request, f"{SETUP}#cost", _("Essential monthly cost saved."))


async def _save_income(request: Request, sources: list[dict], message: str):
    db = get_user_db_from_request(request)
    try:
        await finance.set_assumptions(db, {"income_sources": sources})
    except FinanceError as exc:
        return error_redirect(request, f"{SETUP}#income", localized(exc))
    return success_redirect(request, f"{SETUP}#income", message)


def _source_payload(source: dict) -> dict:
    return {
        "name": source["name"],
        "monthly_amount": source["monthly_amount"],
        "independent_of_work": source["independent_of_work"],
        "confirmed_on": source["confirmed_on"],
        "note": source["note"],
    }


@router.post("/finance/income")
async def add_income(
    request: Request,
    name: str = Form(...),
    monthly_amount: str = Form(...),
    independent_of_work: str = Form(""),
):
    db = get_user_db_from_request(request)
    current = [_source_payload(source) for source in (await finance.assumptions(db))["income_sources"]]
    current = [source for source in current if source["name"].lower() != name.strip().lower()]
    current.append(
        {
            "name": name,
            "monthly_amount": monthly_amount,
            "independent_of_work": bool(independent_of_work),
            "confirmed_on": date.today().isoformat(),
        }
    )
    return await _save_income(request, current, _("{name} saved.").format(name=name.strip()))


@router.post("/finance/income/{source_id}/delete")
async def delete_income(request: Request, source_id: int):
    db = get_user_db_from_request(request)
    sources = (await finance.assumptions(db))["income_sources"]
    kept = [_source_payload(source) for source in sources if source["id"] != source_id]
    if len(kept) == len(sources):
        return error_redirect(request, f"{SETUP}#income", _("That income source no longer exists."))
    return await _save_income(request, kept, _("Income source removed."))


@router.post("/finance/policies")
async def save_policies(request: Request):
    form = await request.form()
    db = get_user_db_from_request(request)
    policies = []
    for key, value in form.multi_items():
        if key.startswith("policy_"):
            kind = key.removeprefix("policy_")
            status = str(value) or None
            policies.append(
                {"type": kind, "status": status, "confirmed_on": date.today().isoformat() if status else ""}
            )
    new_type = str(form.get("new_policy_type", "")).strip()
    if new_type:
        policies.append({"type": new_type, "status": form.get("new_policy_status") or "present"})
    try:
        await finance.set_assumptions(db, {"policies": policies})
    except FinanceError as exc:
        return error_redirect(request, f"{SETUP}#policies", localized(exc))
    return success_redirect(request, f"{SETUP}#policies", _("Policies saved."))


@router.post("/finance/layers")
async def save_layer(
    request: Request,
    layer_id: str = Form(""),
    name: str = Form(...),
    description: str = Form(""),
    counts_toward_cushion: str = Form(""),
    metric: str = Form(""),
    max_points: str = Form(""),
    thresholds: str = Form(""),
):
    db = get_user_db_from_request(request)
    item: dict = {"description": description, "counts_toward_cushion": bool(counts_toward_cushion)}
    if layer_id.strip():
        item["id"] = int(layer_id)
        item["rename"] = name
    else:
        item["name"] = name
    try:
        if metric:
            try:
                points = int(max_points)
            except ValueError as exc:
                raise FinanceError(N_("Maximum points must be a whole number")) from exc
            item["rule"] = {"metric": metric, "max_points": points, "thresholds": _parse_thresholds(metric, thresholds)}
        else:
            item["rule"] = None
        await finance.set_scoring(db, [item])
    except (FinanceError, FinanceNotFound) as exc:
        return error_redirect(request, f"{SETUP}#layers", localized(exc))
    return success_redirect(request, f"{SETUP}#layers", _("{name} saved.").format(name=name.strip()))


@router.post("/finance/layers/{layer_id}/delete")
async def delete_layer(request: Request, layer_id: int):
    db = get_user_db_from_request(request)
    try:
        await finance.set_scoring(db, remove=[layer_id])
    except (FinanceError, FinanceNotFound) as exc:
        return error_redirect(request, f"{SETUP}#layers", localized(exc))
    return success_redirect(request, f"{SETUP}#layers", _("Layer removed."))
