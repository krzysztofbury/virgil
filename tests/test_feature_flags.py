"""Feature flags must reach the nav. Regression: flags were injected by an outer
middleware that ran before AuthMiddleware set user_db, so request.state.features was
always {} and the Feniks/No Porn link never rendered even with the flag enabled."""

import os
import sqlite3
from pathlib import Path

from conftest import csrf_token

_USERS_DIR = Path(os.environ["VIRGIL_CENTRAL_DB_PATH"]).parent / "users"


def _set_feniks(value: str) -> None:
    db_files = list(_USERS_DIR.glob("*.db"))
    assert len(db_files) == 1, f"Expected one user DB, got {db_files}"
    conn = sqlite3.connect(db_files[0])
    try:
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('feature_no_porn', ?)", (value,))
        conn.commit()
    finally:
        conn.close()


def test_flag_on_renders_nav_link(auth_client):
    _set_feniks("1")
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    assert "/feniks" in resp.text, "enabled feniks flag must render the nav link"
    assert "No Porn" in resp.text


def test_flag_off_hides_nav_link(auth_client):
    _set_feniks("0")
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    assert 'href="/feniks"' not in resp.text, "disabled feniks flag must hide the nav link"


def test_save_features_roundtrip_enables_link(auth_client):
    """POST the checkbox -> flag persists -> nav link appears. Covers save_features + injection."""
    _set_feniks("0")
    token = csrf_token(auth_client, "/settings?tab=general")
    resp = auth_client.post(
        "/settings/features",
        data={"feature_no_porn": "on", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/feniks" in auth_client.get("/training").text
