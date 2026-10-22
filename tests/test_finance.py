"""Finance over the API: the fortress the user defines, a month recorded in one
call, and the cushion and score computed from it (#135).

Amounts are invented. Every expected number below is worked out by hand from
the seeded score rules: cushion 12/6/3/1 months for 40/30/20/10 points,
independent income 50/30/20/10% for 25/20/15/10, policies life 10, health 8,
property 7, vehicle 5, income loss 5 out of 35.
"""

import sqlite3

import pytest
from conftest import user_db_path

KEY = {"X-API-Key": "test-key-123"}
TABLES = (
    "finance_balances",
    "finance_accounts",
    "finance_assumptions",
    "finance_income_sources",
    "finance_policies",
)


@pytest.fixture
def finance(auth_client, monkeypatch):
    monkeypatch.setattr("app.config.API_FINANCE", True)
    conn = sqlite3.connect(user_db_path())
    layers = conn.execute(
        "SELECT id, name, description, counts_toward_cushion, display_order FROM finance_layers"
    ).fetchall()
    rules = conn.execute("SELECT * FROM finance_score_rules").fetchall()
    conn.close()
    yield auth_client
    conn = sqlite3.connect(user_db_path())
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
    conn.execute("DELETE FROM finance_score_rules")
    conn.execute("DELETE FROM finance_layers")
    conn.executemany("INSERT INTO finance_layers VALUES (?, ?, ?, ?, ?)", layers)
    conn.executemany("INSERT INTO finance_score_rules VALUES (?, ?, ?, ?)", rules)
    conn.commit()
    conn.close()


def _account(client, **fields):
    response = client.put("/api/finance/accounts", json=fields, headers=KEY)
    assert response.status_code == 200, response.text
    return response.json()


def _setup(client):
    _account(client, name="Joint current", layer="Cash cushion", kind="cash", ownership="joint")
    _account(client, name="Savings", layer="Cash cushion", kind="cash")
    _account(client, name="Bonds", layer="Cash cushion", kind="bonds")
    _account(client, name="Children's bonds", layer="Cash cushion", kind="bonds", ownership="excluded")
    _account(client, name="Retirement", layer="Investments", kind="retirement")
    response = client.put(
        "/api/finance/assumptions",
        json={
            "essential_monthly_cost": 10000,
            "effective_from": "2026-01-01",
            "income_sources": [
                {"name": "Rent", "monthly_amount": 3000, "independent_of_work": True},
                {"name": "Salary", "monthly_amount": 9000},
            ],
            "policies": [
                {"type": "life", "status": "absent"},
                {"type": "health", "status": "present"},
                {"type": "property", "status": "present"},
                {"type": "vehicle", "status": "present"},
                {"type": "income_loss", "status": "not_applicable"},
            ],
        },
        headers=KEY,
    )
    assert response.status_code == 200, response.text


def _record(client, day, entries):
    return client.put(f"/api/finance/balances/{day}", json={"entries": entries}, headers=KEY)


def test_a_recorded_month_gives_the_cushion_and_the_score(finance):
    _setup(finance)
    response = _record(
        finance,
        "2026-09-30",
        [
            {"account": "Joint current", "amount": "10000.10"},
            {"account": "Savings", "amount": 55000},
            {"account": "Bonds", "amount": 60000.4},
            {"account": "children's bonds", "amount": 150000},
            {"account": "Retirement", "amount": 200000},
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["recorded"] == 5

    overview = finance.get("/api/finance", params={"date": "2026-10-15"}, headers=KEY).json()
    assert overview["cushion"] == 125000.5
    assert overview["cushion_months"] == 12.5
    assert overview["total"] == 325000.5
    assert overview["excluded_total"] == 150000
    assert overview["independent_income_share"] == 30.0
    assert overview["stable_income_share"] == 120.0
    assert overview["stale_accounts"] == []
    scores = {layer["name"]: layer["score"] for layer in overview["layers"]}
    assert scores["Cash cushion"]["points"] == 40
    assert scores["Independent income"]["points"] == 20
    # Counting the salary too: 120% of essential cost.
    assert scores["Independent income"]["points_with_all_income"] == 25
    # Income loss does not apply: 20 of the 30 applicable points, scaled to 35.
    assert scores["Protection"]["points"] == 23
    assert scores["Investments"] is None
    assert overview["score"] == {"points": 83, "points_with_all_income": 88, "known_points": 83, "max_points": 100}


def test_a_later_partial_month_carries_balances_and_marks_them_stale(finance):
    _setup(finance)
    _record(finance, "2026-08-31", [{"account": "Savings", "amount": 40000}, {"account": "Bonds", "amount": 20000}])
    finance.put(
        "/api/finance/assumptions",
        json={"essential_monthly_cost": 15000, "effective_from": "2026-09-01"},
        headers=KEY,
    )
    _record(finance, "2026-09-30", [{"account": "Savings", "amount": 45000}])

    overview = finance.get("/api/finance", params={"date": "2026-09-30"}, headers=KEY).json()
    assert overview["cushion"] == 65000
    assert overview["stale_accounts"] == ["Bonds"], "accounts with no balance yet have nothing to carry"

    points = finance.get("/api/finance/history", params={"months": 240}, headers=KEY).json()["points"]
    assert [(p["date"], p["cushion"], p["cushion_months"]) for p in points] == [
        ("2026-08-31", 60000, 6.0),
        ("2026-09-30", 65000, round(65000 / 15000, 2)),
    ], "each month is measured against the cost that applied then"


def test_one_unknown_account_records_nothing(finance):
    _setup(finance)
    response = _record(finance, "2026-09-30", [{"account": "Savings", "amount": 1}, {"account": "Nope", "amount": 2}])
    assert response.status_code == 404
    conn = sqlite3.connect(user_db_path())
    count = conn.execute("SELECT COUNT(*) FROM finance_balances").fetchone()[0]
    conn.close()
    assert count == 0


def test_a_missing_input_leaves_the_score_unknown_not_zero(finance):
    _account(finance, name="Savings", layer="Cash cushion")
    _record(finance, "2026-09-30", [{"account": "Savings", "amount": 1000}])
    overview = finance.get("/api/finance", params={"date": "2026-09-30"}, headers=KEY).json()
    assert overview["cushion_months"] is None
    assert overview["score"]["points"] is None
    cushion = next(layer for layer in overview["layers"] if layer["name"] == "Cash cushion")
    assert cushion["score"]["points"] is None
    assert "essential monthly cost" in cushion["score"]["missing"]


def test_the_user_redefines_the_fortress(finance):
    response = finance.put(
        "/api/finance/scoring",
        json={
            "layers": [
                {
                    "name": "Cash cushion",
                    "rename": "Citadel",
                    "rule": {"metric": "cushion_months", "max_points": 50, "thresholds": [[6, 50], [3, 25]]},
                },
                {"name": "Moat", "description": "Short bonds", "counts_toward_cushion": True},
            ]
        },
        headers=KEY,
    )
    assert response.status_code == 200, response.text
    layers = {layer["name"]: layer for layer in response.json()["layers"]}
    assert layers["Citadel"]["rule"]["thresholds"] == [[6, 50], [3, 25]]
    assert layers["Moat"]["counts_toward_cushion"] is True and layers["Moat"]["rule"] is None
    assert response.json()["max_points"] == 110

    bad = finance.put(
        "/api/finance/scoring",
        json={
            "layers": [
                {"name": "Citadel", "rule": {"metric": "cushion_months", "max_points": 10, "thresholds": [[6, 50]]}}
            ]
        },
        headers=KEY,
    )
    assert bad.status_code == 422, "points above the layer's maximum"

    _account(finance, name="Short bonds", layer="Moat")
    used = finance.put("/api/finance/scoring", json={"remove": ["Moat"]}, headers=KEY)
    assert used.status_code == 422
    assert "still has accounts" in used.json()["detail"]


def test_a_closed_account_stops_counting_after_its_last_balance(finance):
    _setup(finance)
    _record(finance, "2026-08-31", [{"account": "Savings", "amount": 30000}, {"account": "Bonds", "amount": 10000}])
    _account(finance, name="Bonds", archived=True)
    _record(finance, "2026-09-30", [{"account": "Savings", "amount": 31000}])
    points = finance.get("/api/finance/history", params={"months": 240}, headers=KEY).json()["points"]
    assert [p["cushion"] for p in points] == [40000, 31000]


def test_amounts_are_exact(finance):
    _account(finance, name="A", layer="Cash cushion")
    _account(finance, name="B", layer="Cash cushion")
    _record(finance, "2026-09-30", [{"account": "A", "amount": 0.1}, {"account": "B", "amount": "0,2"}])
    overview = finance.get("/api/finance", params={"date": "2026-09-30"}, headers=KEY).json()
    assert overview["cushion"] == 0.3
    bad = _record(finance, "2026-09-30", [{"account": "A", "amount": "abc"}])
    assert bad.status_code == 422


def test_the_monthly_update_works_over_mcp(finance, client, monkeypatch):
    """The point of the module: a month recorded and read back through MCP tools alone."""
    import json

    from fastapi.testclient import TestClient

    from app.main import app
    from mcp_server import virgil_mcp

    rest = TestClient(app, headers=KEY)
    rest.portal = client.portal
    monkeypatch.setattr(virgil_mcp, "_client", rest)

    def call(name, arguments):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        response = client.post("/mcp", headers={"Accept": "application/json, text/event-stream", **KEY}, json=body)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is False, result
        return json.loads(result["content"][0]["text"])

    call("upsert_finance_account", {"name": "2024", "layer": "Cash cushion"})
    call("upsert_finance_account", {"name": "Savings", "layer": "Cash cushion"})
    call("upsert_finance_account", {"name": "Savings", "rename": "Emergency fund"})
    call("set_finance_assumptions", {"essential_monthly_cost": 5000, "effective_from": "2026-01-01"})
    entries = [{"account": "Emergency fund", "amount": 30000}, {"account": "2024", "amount": 5}]
    recorded = call("record_balances", {"date": "2026-09-30", "entries": entries})
    assert recorded["recorded"] == 2, "a name made of digits is a name, not an id"
    overview = call("get_finance_overview", {"date": "2026-09-30"})
    assert (overview["cushion"], overview["cushion_months"]) == (30005, 6.0)
    scoring = call("get_finance_scoring", {})
    assert [layer["name"] for layer in scoring["layers"]][:1] == ["Cash cushion"]
