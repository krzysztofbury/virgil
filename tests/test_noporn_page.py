"""No Porn page layout guards (single-flow redesign): weekly clean-rate bar
(Gola 75%) stays, retired elements stay gone (streak chart, Milestones — and
since the single-flow redesign also the Journal/Pleasures tabs)."""

import os
import sqlite3
from pathlib import Path

_USERS_DIR = Path(os.environ["VIRGIL_CENTRAL_DB_PATH"]).parent / "users"


def _enable_no_porn() -> None:
    db_files = list(_USERS_DIR.glob("*.db"))
    assert len(db_files) == 1, f"Expected one user DB, got {db_files}"
    conn = sqlite3.connect(db_files[0])
    try:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_no_porn', '1')")
        conn.commit()
    finally:
        conn.close()


def test_weekly_bar_present(auth_client):
    _enable_no_porn()
    html = auth_client.get("/feniks").text
    assert "This week:" in html, "weekly clean-rate bar must be shown"
    assert "Target 75%" in html


def test_removed_elements_gone(auth_client):
    _enable_no_porn()
    html = auth_client.get("/feniks").text
    assert "Streak Progress" not in html, "streak chart must stay removed"
    assert "feniksTrendChart" not in html
    assert "Milestones" not in html, "Milestones tab must stay removed"
    assert "tab = 'journal'" not in html, "tab navigation retired by the single-flow redesign"
    assert "tab = 'pleasures'" not in html
