"""Invites (#94): how someone outside the household gets an account while registration stays closed."""

import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import _TMP, csrf_token

PASSWORD = "invitee-password-123"  # pragma: allowlist secret - synthetic fixture


def _central():
    return sqlite3.connect(Path(_TMP) / "central.db")


def _remove_account(email: str) -> None:
    """Delete an account a test created, with its database: conftest expects exactly one user file."""
    central = _central()
    try:
        row = central.execute("SELECT db_filename FROM users WHERE email = ?", (email,)).fetchone()
        central.execute("DELETE FROM users WHERE email = ?", (email,))
        central.commit()
    finally:
        central.close()
    if row:
        for suffix in ("", "-wal", "-shm"):
            Path(_TMP, "users", row[0] + suffix).unlink(missing_ok=True)


def _create_invite(auth_client, **fields) -> tuple[str, object]:
    token = csrf_token(auth_client, "/admin/users")
    response = auth_client.post("/admin/invites", data={**fields, "_csrf_token": token})
    assert response.status_code == 200
    link = re.search(r'value="(http[^"]+/signup\?invite=([^"]+))"', response.text)
    assert link, "the new link is shown once, on the response"
    return link.group(2), response


def _signup(client, invite: str, email: str):
    client.cookies.clear()
    token = csrf_token(client, f"/signup?invite={invite}")
    return client.post(
        "/signup",
        data={
            "email": email,
            "display_name": "Invitee",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "invite": invite,
            "_csrf_token": token,
        },
        follow_redirects=False,
    )


def _err(response) -> str:
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["err"][0]


def test_an_invite_creates_one_account_and_cannot_be_used_twice(auth_client, client):
    invite, created = _create_invite(auth_client, email="Invitee@Example.com", role="admin", allow_shared_llm="1")
    try:
        assert created.headers["cache-control"] == "no-store"
        central = _central()
        stored = central.execute("SELECT token_hash, email FROM invites").fetchall()
        central.close()
        assert stored and all(invite not in row[0] for row in stored), "only a hash of the secret is kept"

        client.cookies.clear()
        page = client.get(f"/signup?invite={invite}")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert 'value="invitee@example.com"' in page.text and "readonly" in page.text

        wrong = _signup(client, invite, "someone-else@example.com")
        assert wrong.status_code == 200 and "This invite is for a different email address." in wrong.text

        made = _signup(client, invite, "invitee@example.com")
        assert made.status_code == 303 and made.headers["location"].startswith("/onboarding")
        central = _central()
        user = central.execute(
            "SELECT id, role, allow_shared_llm FROM users WHERE email = 'invitee@example.com'"
        ).fetchone()
        used = central.execute("SELECT used_by FROM invites WHERE email = 'invitee@example.com'").fetchone()
        central.close()
        assert user[1:] == ("admin", 1), "the invite's role and LLM choice carry over"
        assert used == (user[0],)

        assert _err(_signup(client, invite, "invitee@example.com")) == (
            "This invite link is invalid, used or expired. Ask for a new one."
        )
    finally:
        client.cookies.clear()
        _remove_account("invitee@example.com")


def test_an_invite_without_an_address_gives_a_plain_account_without_the_server_key(auth_client, client):
    invite, _ = _create_invite(auth_client, email="", role="user")
    try:
        assert _signup(client, invite, "anyone@example.com").status_code == 303
        central = _central()
        user = central.execute("SELECT role, allow_shared_llm FROM users WHERE email = 'anyone@example.com'").fetchone()
        central.close()
        assert user == ("user", 0)
    finally:
        client.cookies.clear()
        _remove_account("anyone@example.com")


@pytest.mark.parametrize("how", ["revoked", "expired"])
def test_a_revoked_or_expired_invite_creates_nothing(auth_client, client, how):
    invite, _ = _create_invite(auth_client, email="late@example.com")
    central = _central()
    invite_id = central.execute("SELECT id FROM invites WHERE email = 'late@example.com'").fetchone()[0]
    if how == "revoked":
        token = csrf_token(auth_client, "/admin/users")
        response = auth_client.post(f"/admin/invites/{invite_id}/revoke", data={"_csrf_token": token})
        assert response.status_code == 200
    else:
        central.execute("UPDATE invites SET expires_at = '2020-01-01T00:00:00Z' WHERE id = ?", (invite_id,))
        central.commit()
    try:
        client.cookies.clear()
        page = client.get(f"/signup?invite={invite}")
        assert "This invite link is invalid, used or expired." in page.text
        assert 'name="invite"' not in page.text
        assert _err(_signup(client, invite, "late@example.com")).startswith("This invite link is invalid")
        assert central.execute("SELECT 1 FROM users WHERE email = 'late@example.com'").fetchone() is None
    finally:
        central.execute("DELETE FROM invites WHERE email = 'late@example.com'")
        central.commit()
        central.close()
        client.cookies.clear()


def test_signup_without_an_invite_stays_closed(client):
    client.cookies.clear()
    token = csrf_token(client, "/login")
    response = client.post(
        "/signup",
        data={
            "email": "walk-in@example.com",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert _err(response) == "Registration is closed on this server."


def _insert_admin(email: str) -> str:
    import uuid

    user_id = str(uuid.uuid4())
    central = _central()
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename, role) VALUES (?, ?, 'x', ?, 'admin')",
        (user_id, email, f"{user_id}.db"),
    )
    central.commit()
    central.close()
    return user_id


def test_roles_change_except_for_break_glass_admins(auth_client, monkeypatch):
    import app.routers.admin as admin

    other = _insert_admin("colleague@example.com")
    protected = _insert_admin("operator@example.com")
    monkeypatch.setattr(admin, "ADMIN_EMAILS", [*admin.ADMIN_EMAILS, "operator@example.com"])
    central = _central()
    try:

        def post(path: str, **data):
            token = csrf_token(auth_client, "/admin/users")
            return auth_client.post(path, data={**data, "_csrf_token": token}, follow_redirects=False)

        assert post(f"/admin/users/{other}/role", role="user").status_code == 303
        assert central.execute("SELECT role FROM users WHERE id = ?", (other,)).fetchone() == ("user",)

        refusals = [
            _err(post(f"/admin/users/{protected}/role", role="user")),
            _err(post(f"/admin/users/{protected}/disable")),
            _err(post(f"/admin/users/{protected}/delete")),
        ]
        assert refusals == [
            "operator@example.com is in VIRGIL_ADMIN_EMAILS and stays an admin.",
            "operator@example.com is in VIRGIL_ADMIN_EMAILS and cannot be disabled.",
            "operator@example.com is in VIRGIL_ADMIN_EMAILS and cannot be deleted.",
        ]
        assert central.execute("SELECT role, is_active FROM users WHERE id = ?", (protected,)).fetchone() == (
            "admin",
            1,
        )

        me = central.execute("SELECT id FROM users WHERE email = 'test@example.com'").fetchone()[0]
        assert _err(post(f"/admin/users/{me}/role", role="user")) == (
            "Your own role cannot be changed from the admin panel."
        )
    finally:
        central.execute("DELETE FROM users WHERE id IN (?, ?)", (other, protected))
        central.commit()
        central.close()


def test_the_access_log_keeps_no_invite_secret(caplog):
    from app.main import RedactLinkSecrets

    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, RedactLinkSecrets) for f in access.filters)
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        access.info('%s - "%s %s HTTP/%s" %d', "203.0.113.9", "GET", "/signup?invite=s3cr3t-Value_1&x=1", "1.1", 200)
    assert "s3cr3t" not in caplog.text
    assert "/signup?invite=[redacted]&x=1" in caplog.text
