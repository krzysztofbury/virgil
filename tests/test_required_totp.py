"""The "require two-factor with passwords" switch and the admin reset (#168)."""

import asyncio
import sqlite3
import uuid
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL, csrf_token
from fastapi.testclient import TestClient

from app import config
from app.central_db import count_recovery_codes, create_invite, replace_recovery_codes


def _central():
    conn = sqlite3.connect(Path(_TMP) / "central.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def make_account(auth_client, client):
    """Sign up fresh accounts through invites; each gets its own browser."""
    from app.main import app

    made: list[tuple[str, TestClient]] = []
    central = _central()
    admin_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()["id"]

    def make(*, password: str | None = "member-password-1", mail: bool = False):  # pragma: allowlist secret
        email = f"gate-{uuid.uuid4().hex[:8]}@example.com"
        browser = TestClient(app)
        browser.portal = client.portal
        invite = asyncio.run(create_invite(admin_id, email=email))
        data = {"email": email, "invite": invite, "_csrf_token": csrf_token(browser, f"/signup?invite={invite}")}
        if password is None:
            data["sign_in_method"] = "link"
        else:
            data |= {"password": password, "password_confirm": password, "sign_in_method": "password"}
        response = browser.post("/signup", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text[:300]
        user = central.execute("SELECT id, db_filename FROM users WHERE email = ?", (email,)).fetchone()
        user_db = sqlite3.connect(Path(_TMP) / "users" / user["db_filename"])
        user_db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('onboarding_completed', '1')")
        user_db.commit()
        user_db.close()
        made.append((email, browser))
        return user["id"], browser

    try:
        yield make
    finally:
        for email, browser in made:
            browser.close()
            row = central.execute("SELECT id, db_filename FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                central.execute("DELETE FROM users WHERE id = ?", (row["id"],))
                (Path(_TMP) / "users" / row["db_filename"]).unlink(missing_ok=True)
        central.commit()
        central.close()


def test_with_the_switch_off_nothing_changes(make_account, monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_TOTP", False)
    _user_id, browser = make_account()
    assert browser.get("/", follow_redirects=False).status_code == 200


def test_a_password_account_without_totp_must_set_it_up(make_account, monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_TOTP", True)
    _user_id, browser = make_account()
    for path in ("/", "/daily", "/settings"):
        response = browser.get(path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/settings/mfa", path
    assert browser.get("/settings/mfa").status_code == 200
    htmx = browser.get("/daily", headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.headers.get("HX-Redirect") == "/settings/mfa"
    logout = browser.post("/logout", data={"_csrf_token": csrf_token(browser, "/settings/mfa")}, follow_redirects=False)
    assert logout.status_code == 303 and logout.headers["location"].startswith("/login")


def test_a_link_only_account_is_not_gated(make_account, monkeypatch, sent_mail):
    monkeypatch.setattr(config, "REQUIRE_TOTP", True)
    _user_id, browser = make_account(password=None)
    assert browser.get("/", follow_redirects=False).status_code == 200


def test_reset_two_factor_signs_out_and_voids_codes(auth_client, make_account):
    user_id, browser = make_account()
    central = _central()
    central.execute("UPDATE users SET totp_enabled = 1, totp_secret = 'x' WHERE id = ?", (user_id,))
    central.commit()
    asyncio.run(replace_recovery_codes(user_id))
    epoch = central.execute("SELECT session_epoch FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    response = auth_client.post(
        f"/admin/users/{user_id}/reset-two-factor",
        data={"_csrf_token": csrf_token(auth_client, "/admin/users")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = central.execute(
        "SELECT totp_enabled, totp_secret, session_epoch FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    assert (row["totp_enabled"], row["totp_secret"]) == (0, "")
    assert row["session_epoch"] == epoch + 1
    assert asyncio.run(count_recovery_codes(user_id)) == 0
    assert (
        central.execute(
            "SELECT COUNT(*) FROM auth_events WHERE user_id = ? AND event = 'mfa_admin_reset'", (user_id,)
        ).fetchone()[0]
        == 1
    )
    assert browser.get("/", follow_redirects=False).headers["location"] == "/login"
    central.close()


def test_only_an_admin_can_reset_two_factor(auth_client, make_account):
    target_id, _target = make_account()
    _member_id, member = make_account()
    response = member.post(
        f"/admin/users/{target_id}/reset-two-factor",
        data={"_csrf_token": csrf_token(member, "/settings")},
        follow_redirects=False,
    )
    assert response.status_code in (303, 403)
    assert response.headers.get("location", "") != "/admin/users"


def test_an_admin_resets_their_own_two_factor_in_settings(auth_client):
    central = _central()
    admin_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()["id"]
    central.close()
    response = auth_client.post(
        f"/admin/users/{admin_id}/reset-two-factor",
        data={"_csrf_token": csrf_token(auth_client, "/admin/users")},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]


def test_a_break_glass_admin_keeps_their_two_factor(auth_client, make_account, monkeypatch):
    """Like a reset link, a two-factor reset would let any admin take over the
    operator's own account in VIRGIL_ADMIN_EMAILS."""
    import app.routers.admin as admin

    user_id, _browser = make_account()
    central = _central()
    email = central.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()["email"]
    central.execute("UPDATE users SET totp_enabled = 1, totp_secret = 'x' WHERE id = ?", (user_id,))
    central.commit()
    monkeypatch.setattr(admin, "ADMIN_EMAILS", [*admin.ADMIN_EMAILS, email])
    response = auth_client.post(
        f"/admin/users/{user_id}/reset-two-factor",
        data={"_csrf_token": csrf_token(auth_client, "/admin/users")},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]
    assert central.execute("SELECT totp_enabled FROM users WHERE id = ?", (user_id,)).fetchone()[0] == 1
    central.close()
