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
