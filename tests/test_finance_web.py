"""The Finance pages: behind their module switch, and every form writes
through the same service the API uses (#135)."""

import sqlite3

import pytest
from conftest import csrf_token, user_db_path


def _set_flag(value: str) -> None:
    conn = sqlite3.connect(user_db_path())
    conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('feature_finance', ?)", (value,))
    conn.commit()
    conn.close()


@pytest.fixture
def finance_on(auth_client):
    conn = sqlite3.connect(user_db_path())
    layers = conn.execute("SELECT * FROM finance_layers").fetchall()
    rules = conn.execute("SELECT * FROM finance_score_rules").fetchall()
    conn.close()
    _set_flag("1")
    yield auth_client
    _set_flag("0")
    conn = sqlite3.connect(user_db_path())
    for table in (
        "finance_balances",
        "finance_accounts",
        "finance_assumptions",
        "finance_income_sources",
        "finance_policies",
        "finance_score_rules",
        "finance_layers",
    ):
        conn.execute(f"DELETE FROM {table}")  # noqa: S608
    conn.executemany("INSERT INTO finance_layers VALUES (?, ?, ?, ?, ?)", layers)
    conn.executemany("INSERT INTO finance_score_rules VALUES (?, ?, ?, ?)", rules)
    conn.commit()
    conn.close()


def _post(client, path, data, page="/finance/setup"):
    return client.post(path, data={"_csrf_token": csrf_token(client, page), **data}, follow_redirects=False)


def _layer_id(name):
    conn = sqlite3.connect(user_db_path())
    try:
        return conn.execute("SELECT id FROM finance_layers WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def test_the_pages_follow_the_module_switch(auth_client):
    _set_flag("0")
    assert auth_client.get("/finance", follow_redirects=False).status_code == 303
    assert 'href="/finance"' not in auth_client.get("/").text

    token = csrf_token(auth_client, "/settings?tab=general")
    auth_client.post("/settings/features", data={"_csrf_token": token, "feature_finance": "1"}, follow_redirects=False)
    try:
        assert auth_client.get("/finance").status_code == 200
        assert 'href="/finance"' in auth_client.get("/").text
    finally:
        _set_flag("0")


def test_setting_up_and_recording_a_month_from_the_pages(finance_on):
    client = finance_on
    added = _post(
        client,
        "/finance/accounts",
        {"name": "ZZ Savings", "layer_id": _layer_id("Cash cushion"), "kind": "cash", "ownership": "own"},
    )
    assert "err=" not in added.headers["location"]
    _post(client, "/finance/cost", {"essential_monthly_cost": "4 000,00", "effective_from": "2026-01-01"})
    _post(client, "/finance/income", {"name": "ZZ Rent", "monthly_amount": "1000", "independent_of_work": "1"})
    _post(
        client,
        "/finance/policies",
        {
            "policy_life": "present",
            "policy_health": "present",
            "policy_property": "absent",
            "policy_vehicle": "not_applicable",
            "policy_income_loss": "absent",
        },
    )

    conn = sqlite3.connect(user_db_path())
    account_id = conn.execute("SELECT id FROM finance_accounts WHERE name = 'ZZ Savings'").fetchone()[0]
    conn.close()
    saved = _post(
        client, "/finance/balances", {"date": "2026-09-30", f"balance_{account_id}": "24000"}, page="/finance"
    )
    assert "err=" not in saved.headers["location"]

    page = client.get("/finance").text
    assert "24,000.00" in page
    assert "Record balances" in page
    # 24 000 / 4 000 = 6 months -> 30; rent 25% -> 15; policies 18 of 30 applicable -> 21.
    assert "66<small>/100</small>" in page


def test_a_layer_takes_thresholds_written_as_text(finance_on):
    client = finance_on
    response = _post(
        client,
        "/finance/layers",
        {
            "name": "ZZ Moat",
            "counts_toward_cushion": "1",
            "metric": "cushion_months",
            "max_points": "20",
            "thresholds": "6:20, 3:10",
        },
    )
    assert "err=" not in response.headers["location"]
    conn = sqlite3.connect(user_db_path())
    rule = conn.execute(
        "SELECT r.thresholds FROM finance_score_rules r JOIN finance_layers l ON l.id = r.layer_id WHERE l.name = 'ZZ Moat'"
    ).fetchone()
    conn.close()
    assert rule == ("[[6, 20], [3, 10]]",)

    bad = _post(
        client,
        "/finance/layers",
        {"name": "ZZ Moat 2", "metric": "cushion_months", "max_points": "20", "thresholds": "six"},
    )
    assert "err=" in bad.headers["location"]


def test_record_balances_prefills_grouped_amounts_that_save_back(finance_on):
    """A bare 950000.00 in the form next to 9,500.00 on the cards invited an
    order-of-magnitude typo (#163). The field groups thousands with spaces, and
    an amount pasted with a no-break space, as Polish banks show it, saves."""
    client = finance_on
    _post(client, "/finance/accounts", {"name": "ZZ Grouped", "layer_id": _layer_id("Cash cushion"), "kind": "cash"})
    conn = sqlite3.connect(user_db_path())
    account_id = conn.execute("SELECT id FROM finance_accounts WHERE name = 'ZZ Grouped'").fetchone()[0]
    conn.close()
    _post(client, "/finance/balances", {"date": "2026-09-30", f"balance_{account_id}": "9500"}, page="/finance")
    assert (
        f'name="balance_{account_id}"\n                               value="9 500.00"' in client.get("/finance").text
    )

    saved = _post(
        client, "/finance/balances", {"date": "2026-10-01", f"balance_{account_id}": "12 345,67"}, page="/finance"
    )
    assert "err=" not in saved.headers["location"]
    conn = sqlite3.connect(user_db_path())
    amount = conn.execute(
        "SELECT amount_minor FROM finance_balances WHERE account_id = ? AND date = '2026-10-01'", (account_id,)
    ).fetchone()[0]
    conn.close()
    assert amount == 1234567


def test_the_cushion_chart_names_its_unit(finance_on):
    """A bare 0-10 axis under "Cushion over time", next to a cushion of ~90k PLN,
    read as money (#163). The unit is months of essential cost."""
    client = finance_on
    _post(client, "/finance/accounts", {"name": "ZZ Chart", "layer_id": _layer_id("Cash cushion"), "kind": "cash"})
    _post(client, "/finance/cost", {"essential_monthly_cost": "4000", "effective_from": "2026-01-01"})
    conn = sqlite3.connect(user_db_path())
    account_id = conn.execute("SELECT id FROM finance_accounts WHERE name = 'ZZ Chart'").fetchone()[0]
    conn.close()
    for day, amount in (("2026-08-31", "20000"), ("2026-09-30", "24000")):
        _post(client, "/finance/balances", {"date": day, f"balance_{account_id}": amount}, page="/finance")
    page = client.get("/finance").text
    assert "Cushion over time, in months of essential cost" in page
    assert 'title: { display: true, text: "Months" }' in page
