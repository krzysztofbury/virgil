"""Admin-issued password reset links (#94): there is no mailer, so an admin hands the link over."""

import re
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import _TMP, csrf_token

OLD = "member-old-password-1"  # pragma: allowlist secret - synthetic fixture
NEW = "member-new-password-2"  # pragma: allowlist secret - synthetic fixture
UNUSABLE = "This reset link is invalid, used or expired. Ask an admin for a new one."


@pytest.fixture
def member():
    from app.auth import hash_password

    user_id = str(uuid.uuid4())
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, 'member@example.com', ?, ?)",
        (user_id, hash_password(OLD), f"{user_id}.db"),
    )
    central.commit()
    try:
        yield user_id, central
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.commit()
        central.close()


def _admin_post(auth_client, path: str):
    token = csrf_token(auth_client, "/admin/users")
    return auth_client.post(path, data={"_csrf_token": token}, follow_redirects=False)


def _reset_link(auth_client, user_id: str) -> str:
    response = _admin_post(auth_client, f"/admin/users/{user_id}/reset-link")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    match = re.search(r'value="http[^"]+/reset\?token=([^"]+)"', response.text)
    assert match, "the link is shown once, on the response"
    return match.group(1)


def _submit(client, token: str, password: str, confirm: str | None = None):
    client.cookies.clear()
    csrf = csrf_token(client, f"/reset?token={token}")
    return client.post(
        "/reset",
        data={
            "token": token,
            "password": password,
            "password_confirm": password if confirm is None else confirm,
            "_csrf_token": csrf,
        },
        follow_redirects=False,
    )


def _query(response) -> dict:
    assert response.status_code == 303
    return {key: values[0] for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()}


def _signs_in(client, password: str) -> bool:
    client.cookies.clear()
    csrf = csrf_token(client, "/login")
    response = client.post(
        "/login",
        data={"username": "member@example.com", "password": password, "_csrf_token": csrf},
        follow_redirects=False,
    )
    client.cookies.clear()
    return response.status_code == 303


def test_a_reset_link_sets_the_password_once_and_signs_out_every_session(auth_client, client, member):
    import app.rate_limit as rate_limit
    from app.auth import SESSION_COOKIE, create_session

    user_id, central = member
    old_session = create_session(user_id, 0)
    token = _reset_link(auth_client, user_id)
    try:
        assert all(token not in row[0] for row in central.execute("SELECT token_hash FROM password_resets"))

        client.cookies.clear()
        page = client.get(f"/reset?token={token}")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert "Set a new password for member@example.com." in page.text

        mismatch = _submit(client, token, NEW, "something-else-3")
        assert mismatch.status_code == 200 and "Passwords do not match." in mismatch.text

        done = _submit(client, token, NEW)
        assert _query(done) == {"msg": "Password set. Sign in with the new one."}
        assert _signs_in(client, NEW) and not _signs_in(client, OLD)

        client.cookies.set(SESSION_COOKIE, old_session)
        assert client.get("/", follow_redirects=False).headers["location"] == "/login", "old sessions end"
        client.cookies.clear()

        # Ten sign-in requests a minute per client: this test alone has made that many.
        rate_limit._buckets.clear()
        assert _query(_submit(client, token, "yet-another-one-4")) == {"err": UNUSABLE}
    finally:
        client.cookies.clear()


def test_only_the_newest_unexpired_link_works(auth_client, client, member):
    user_id, central = member
    first = _reset_link(auth_client, user_id)
    second = _reset_link(auth_client, user_id)
    assert _query(_submit(client, first, NEW)) == {"err": UNUSABLE}

    central.execute("UPDATE password_resets SET expires_at = '2020-01-01T00:00:00Z' WHERE user_id = ?", (user_id,))
    central.commit()
    client.cookies.clear()
    assert UNUSABLE in client.get(f"/reset?token={second}").text
    assert _query(_submit(client, second, NEW)) == {"err": UNUSABLE}
    assert _signs_in(client, OLD)


def test_no_link_for_yourself_a_break_glass_admin_or_a_disabled_account(auth_client, member, monkeypatch):
    import app.routers.admin as admin

    user_id, central = member
    me = central.execute("SELECT id FROM users WHERE email = 'test@example.com'").fetchone()[0]
    assert _query(_admin_post(auth_client, f"/admin/users/{me}/reset-link"))["err"] == (
        "Change your own password under Settings > Security."
    )

    monkeypatch.setattr(admin, "ADMIN_EMAILS", [*admin.ADMIN_EMAILS, "member@example.com"])
    assert _query(_admin_post(auth_client, f"/admin/users/{user_id}/reset-link"))["err"] == (
        "member@example.com is in VIRGIL_ADMIN_EMAILS; only its owner sets its password."
    )
    monkeypatch.undo()

    central.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    central.commit()
    assert _query(_admin_post(auth_client, f"/admin/users/{user_id}/reset-link"))["err"] == (
        "Enable the account before resetting its password."
    )
    assert central.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ?", (user_id,)).fetchone() == (0,)
