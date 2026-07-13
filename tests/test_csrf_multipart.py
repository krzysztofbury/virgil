"""CSRF middleware must accept tokens from multipart forms.

Regression: parse_qs() cannot parse multipart boundaries, so every
multipart/form-data POST (medical-PDF onboarding upload) was rejected 403.
"""

from conftest import csrf_token


def test_multipart_form_token_accepted(auth_client):
    token = csrf_token(auth_client, "/settings")
    resp = auth_client.post(
        "/settings/features",
        data={"_csrf_token": token},
        files={"dummy": ("dummy.txt", b"payload")},  # forces multipart encoding
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text


def test_multipart_without_token_rejected(auth_client):
    resp = auth_client.post(
        "/settings/features",
        data={"other": "1"},
        files={"dummy": ("dummy.txt", b"payload")},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_urlencoded_form_token_still_accepted(auth_client):
    token = csrf_token(auth_client, "/settings")
    resp = auth_client.post(
        "/settings/features",
        data={"_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_oversize_urlencoded_body_413(auth_client):
    token = csrf_token(auth_client, "/settings")
    # 10 MB cap for urlencoded bodies — this one is ~10.5 MB.
    resp = auth_client.post(
        "/settings/features",
        data={"_csrf_token": token, "blob": "x" * (10_500_000)},
        follow_redirects=False,
    )
    assert resp.status_code == 413
