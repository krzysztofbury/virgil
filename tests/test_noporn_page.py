"""No Porn page redesign: weekly clean-rate bar (Gola 75%), no streak chart,
no Milestones tab, client-side tabs. Regression guards for the redesign."""

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
    assert "Streak Progress" not in html, "streak chart must be removed"
    assert "feniksTrendChart" not in html
    assert "Milestones" not in html, "Milestones tab must be removed"


def test_tabs_are_client_side(auth_client):
    """Tab switches happen via Alpine, not a page navigation (no reload/scroll)."""
    _enable_no_porn()
    html = auth_client.get("/feniks").text
    assert "@click.prevent=\"tab = 'journal'\"" in html, "Journal tab must switch client-side"
    assert "@click.prevent=\"tab = 'pleasures'\"" in html, "Pleasures tab must switch client-side"
    assert 'x-show="tab ===' in html
