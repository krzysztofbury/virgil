"""Settings grouped by what a person wants to do, one topic per tab.

The Automation tab mixed backups, Oura sync and the morning briefing under one
form that treated every missing checkbox as off. Each now lives with its topic
and saves on its own, so saving one never switches another off.
"""

import re
import sqlite3

import pytest
from conftest import csrf_token, user_db_path

_GROUPS = {
    "backup_enabled": "1",
    "backup_interval_hours": "12.0",
    "backup_max_copies": "5",
    "oura_sync_enabled": "1",
    "oura_sync_interval_hours": "3.0",
    "briefing_enabled": "1",
}


def _seed(values: dict[str, str]) -> None:
    conn = sqlite3.connect(user_db_path())
    try:
        conn.executemany("INSERT OR REPLACE INTO app_settings(key, value) VALUES(?, ?)", values.items())
        conn.commit()
    finally:
        conn.close()


def _settings() -> dict[str, str]:
    conn = sqlite3.connect(user_db_path())
    try:
        rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
    finally:
        conn.close()
    return {key: value for key, value in rows if key in _GROUPS}


@pytest.mark.parametrize(
    ("form", "changed", "tab"),
    [
        (
            {"backup_shown": "1", "backup_interval_hours": "48", "backup_max_copies": "9"},
            {"backup_enabled": "0", "backup_interval_hours": "48.0", "backup_max_copies": "9"},
            "data",
        ),
        (
            {"oura_sync_shown": "1", "oura_sync_interval_hours": "8"},
            {"oura_sync_enabled": "0", "oura_sync_interval_hours": "8.0"},
            "integrations",
        ),
        ({"briefing_shown": "1"}, {"briefing_enabled": "0"}, "ai"),
    ],
    ids=["backup", "oura-sync", "briefing"],
)
def test_saving_one_group_leaves_the_others_as_they_were(auth_client, form, changed, tab):
    _seed(_GROUPS)
    token = csrf_token(auth_client, f"/settings?tab={tab}")

    response = auth_client.post("/settings/automation", data={**form, "_csrf_token": token}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/settings?tab={tab}")
    assert _settings() == {**_GROUPS, **changed}


def test_an_invalid_value_in_one_group_changes_nothing(auth_client):
    _seed(_GROUPS)
    token = csrf_token(auth_client, "/settings?tab=data")

    response = auth_client.post(
        "/settings/automation",
        data={"backup_shown": "1", "backup_interval_hours": "500", "_csrf_token": token},
        follow_redirects=False,
    )

    assert response.headers["location"].startswith("/settings?tab=data")
    assert _settings() == _GROUPS


def test_an_old_automation_link_opens_the_data_tab(auth_client):
    html = auth_client.get("/settings?tab=automation").text

    active = re.search(r'id="settings-tab-(\w+)"[^>]*class="active"', html)
    assert active and active.group(1) == "data"
    assert "settings-tab-automation" not in html


@pytest.mark.parametrize(
    ("tab", "present", "absent"),
    [
        ("general", ['action="/language"', 'action="/settings/features"'], ["Storage", "Thinking level"]),
        (
            "ai",
            ['action="/settings/llm/reasoning"', 'name="briefing_shown"', 'action="/settings/tokens"'],
            ['action="/settings/features"'],
        ),
        ("integrations", ["Oura"], ["Coming soon"]),
        (
            "data",
            ['name="backup_shown"', 'action="/settings/factory-reset"', 'id="recent-jobs-heading"'],
            ['name="oura_sync_shown"', 'name="briefing_shown"'],
        ),
        ("security", ['href="/settings/mfa"'], ["Sync Log", "factory-reset"]),
    ],
)
def test_each_tab_holds_its_own_topic(auth_client, tab, present, absent):
    html = auth_client.get(f"/settings?tab={tab}").text

    for marker in present:
        assert marker in html, marker
    for marker in absent:
        assert marker not in html, marker
