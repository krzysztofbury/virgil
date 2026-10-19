"""Form routes store dates as YYYY-MM-DD, whatever ISO spelling was posted.

date.fromisoformat also accepts 20261017 and 2026-W42-6. Stored raw, those rows
never matched the string range queries, and a second spelling of the same day
made a second row instead of updating the first.
"""

import sqlite3

import pytest
from conftest import csrf_token, user_db_path

from app.validation import valid_date

# Saturday 17 October 2026, in the spellings Python accepts.
SPELLINGS = ["20261017", "2026-W42-6", "2026W426"]
POSTED = "('20261017', '2026-W42-6', '2026W426', '2026-10-17')"


@pytest.mark.parametrize("spelling", [*SPELLINGS, "2026-10-17"])
def test_valid_date_returns_the_canonical_day(spelling):
    assert valid_date(spelling) == "2026-10-17"


@pytest.mark.parametrize("spelling", ["", "2026-13-01", "yesterday", None])
def test_valid_date_rejects_what_is_not_a_date(spelling):
    assert valid_date(spelling) is None


def _bloodwork_marker(conn) -> int:
    conn.execute(
        "INSERT INTO blood_markers (category, name, unit, display_order) VALUES ('Test', 'DateMarker', 'u', 0)"
    )
    return conn.execute("SELECT id FROM blood_markers WHERE name = 'DateMarker'").fetchone()[0]


ROUTES = [
    ("/daily/save", lambda _conn: {"notes": "x"}, "daily_logs"),
    ("/daily/measurements", lambda _conn: {"weight": "80"}, "body_measurements"),
    ("/life-scores/save", lambda _conn: {"health": "5"}, "life_scores"),
    ("/bloodwork/result", lambda conn: {"marker_id": str(_bloodwork_marker(conn)), "value": "5"}, "blood_results"),
]


@pytest.mark.parametrize(("path", "fields", "table"), ROUTES, ids=[route[0] for route in ROUTES])
def test_form_routes_store_one_canonical_row_per_day(auth_client, path, fields, table):
    conn = sqlite3.connect(user_db_path())
    try:
        data = fields(conn)
        conn.commit()
        token = csrf_token(auth_client, "/daily")
        for spelling in [*SPELLINGS, "2026-10-17"]:
            response = auth_client.post(
                path, data={**data, "date": spelling, "_csrf_token": token}, follow_redirects=False
            )
            assert response.status_code == 303, response.text

        dates = [row[0] for row in conn.execute(f"SELECT date FROM {table} WHERE date IN {POSTED}")]  # noqa: S608
        assert dates == ["2026-10-17"]
    finally:
        conn.execute(f"DELETE FROM {table} WHERE date IN {POSTED}")  # noqa: S608
        conn.execute("DELETE FROM blood_markers WHERE name = 'DateMarker'")
        conn.commit()
        conn.close()
