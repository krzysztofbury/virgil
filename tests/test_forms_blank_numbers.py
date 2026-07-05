"""Regression: blank numeric form fields must not 422 (HTML sends '' for empty inputs)."""

from conftest import csrf_token


def test_life_scores_accepts_blank_numbers(auth_client):
    token = csrf_token(auth_client, "/")
    resp = auth_client.post(
        "/life-scores/save",
        data={
            "date": "2026-07-05",
            "planning": "4",
            "linkedin_followers": "",
            "youtube_views": "",
            "revenue": "",
            "weight": "99,5",  # polski przecinek dziesiętny też ma przejść
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"Expected redirect, got {resp.status_code}: {resp.text[:300]}"
