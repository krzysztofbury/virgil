"""Bloodwork: server-side out-of-range flags + marker existence validation."""

import sqlite3

from conftest import csrf_token, user_db_path

from app.routers.bloodwork import compute_flag


def test_compute_flag_bounds():
    assert compute_flag(3.0, 4.0, 10.0) == "L"
    assert compute_flag(12.0, 4.0, 10.0) == "H"
    assert compute_flag(7.0, 4.0, 10.0) == ""
    assert compute_flag(7.0, None, None) == ""
    # Boundary values are in range, not flagged.
    assert compute_flag(4.0, 4.0, 10.0) == ""
    assert compute_flag(10.0, 4.0, 10.0) == ""


def _marker_id(conn, name: str) -> int:
    return conn.execute("SELECT id FROM blood_markers WHERE name = ?", (name,)).fetchone()[0]


def test_flag_autocomputed_and_override_respected(auth_client):
    token = csrf_token(auth_client, "/bloodwork")
    resp = auth_client.post(
        "/bloodwork/marker",
        data={
            "name": "TestMarker",
            "category": "Test",
            "unit": "mg/dl",
            "ref_low": "4",
            "ref_high": "10",
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = sqlite3.connect(user_db_path())
    try:
        mid = _marker_id(conn, "TestMarker")
    finally:
        conn.close()

    cases = [
        # (date, value, submitted_flag, expected_stored_flag)
        ("2026-01-01", "2.5", "", "L"),
        ("2026-01-02", "15", "", "H"),
        ("2026-01-03", "7", "", ""),
        ("2026-01-04", "7", "H", "H"),  # explicit lab-reported flag wins
    ]
    for day, value, flag, _expected in cases:
        resp = auth_client.post(
            "/bloodwork/result",
            data={"marker_id": str(mid), "date": day, "value": value, "flag": flag, "_csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    conn = sqlite3.connect(user_db_path())
    try:
        for day, _value, _flag, expected in cases:
            stored = conn.execute(
                "SELECT flag FROM blood_results WHERE marker_id = ? AND date = ?", (mid, day)
            ).fetchone()[0]
            assert stored == expected, f"{day}: expected {expected!r}, got {stored!r}"
        conn.execute("DELETE FROM blood_results WHERE marker_id = ?", (mid,))
        conn.execute("DELETE FROM blood_markers WHERE id = ?", (mid,))
        conn.commit()
    finally:
        conn.close()


def test_nonexistent_marker_redirects_not_500(auth_client):
    token = csrf_token(auth_client, "/bloodwork")
    resp = auth_client.post(
        "/bloodwork/result",
        data={"marker_id": "999999", "date": "2026-01-01", "value": "5", "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    conn = sqlite3.connect(user_db_path())
    try:
        n = conn.execute("SELECT COUNT(*) FROM blood_results WHERE marker_id = 999999").fetchone()[0]
        assert n == 0
    finally:
        conn.close()
