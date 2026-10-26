"""Sign-in links and MFA recovery codes are one-time secrets stored as hashes (#168)."""

import asyncio
import sqlite3
import uuid
from pathlib import Path

import pytest
from conftest import _TMP

from app.central_db import (
    RECOVERY_CODE_COUNT,
    SIGN_IN_LINKS_PER_WINDOW,
    count_recovery_codes,
    create_sign_in_link,
    delete_recovery_codes,
    get_open_sign_in_link,
    normalize_recovery_code,
    replace_recovery_codes,
    use_recovery_code,
    use_sign_in_link,
)


@pytest.fixture
def account(client):
    user_id = str(uuid.uuid4())
    email = f"link-{user_id[:8]}@example.com"
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute(
        "INSERT INTO users (id, email, password_hash, db_filename) VALUES (?, ?, '', ?)",
        (user_id, email, f"{user_id}.db"),
    )
    central.commit()
    try:
        yield user_id, email, central
    finally:
        central.execute("DELETE FROM users WHERE id = ?", (user_id,))
        central.execute("DELETE FROM sign_in_links WHERE email = ?", (email,))
        central.commit()
        central.close()


def test_a_link_opens_once(account):
    user_id, email, _central = account
    token = asyncio.run(create_sign_in_link(email, user_id=user_id))
    assert asyncio.run(get_open_sign_in_link(token))["user_id"] == user_id
    first = asyncio.run(use_sign_in_link(token))
    assert first["user_id"] == user_id and first["purpose"] == "sign_in"
    assert asyncio.run(use_sign_in_link(token)) is None
    assert asyncio.run(get_open_sign_in_link(token)) is None


def test_only_the_hash_is_stored(account):
    user_id, email, central = account
    token = asyncio.run(create_sign_in_link(email, user_id=user_id))
    stored = central.execute("SELECT token_hash FROM sign_in_links WHERE email = ?", (email,)).fetchone()[0]
    assert token not in stored and len(stored) == 64


def test_an_expired_link_does_not_open(account):
    user_id, email, central = account
    token = asyncio.run(create_sign_in_link(email, user_id=user_id))
    central.execute("UPDATE sign_in_links SET expires_at = '2000-01-01T00:00:00Z' WHERE email = ?", (email,))
    central.commit()
    assert asyncio.run(get_open_sign_in_link(token)) is None
    assert asyncio.run(use_sign_in_link(token)) is None


def test_the_window_caps_links_per_address(account):
    user_id, email, _central = account
    tokens = [asyncio.run(create_sign_in_link(email, user_id=user_id)) for _ in range(SIGN_IN_LINKS_PER_WINDOW)]
    assert all(tokens)
    assert asyncio.run(create_sign_in_link(email.upper(), user_id=user_id)) is None


def test_a_link_for_a_disabled_account_does_not_open(account):
    user_id, email, central = account
    token = asyncio.run(create_sign_in_link(email, user_id=user_id))
    central.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    central.commit()
    assert asyncio.run(get_open_sign_in_link(token)) is None
    assert asyncio.run(use_sign_in_link(token)) is None


def test_a_signup_link_needs_no_account(client):
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    token = asyncio.run(create_sign_in_link(email, user_id=None, purpose="signup", display_name="Ala", language="pl"))
    row = asyncio.run(use_sign_in_link(token))
    assert (row["purpose"], row["email"], row["display_name"], row["language"]) == ("signup", email, "Ala", "pl")


def test_recovery_codes_work_once_each_and_forgive_format(account):
    user_id, _email, _central = account
    codes = asyncio.run(replace_recovery_codes(user_id))
    assert len(codes) == RECOVERY_CODE_COUNT == 10
    assert len(set(codes)) == 10
    first = codes[0]
    assert len(first) == 11 and first[5] == "-"
    sloppy = " " + first.upper().replace("-", " ") + " "
    assert normalize_recovery_code(sloppy) == normalize_recovery_code(first)
    assert asyncio.run(use_recovery_code(user_id, sloppy)) is True
    assert asyncio.run(use_recovery_code(user_id, first)) is False
    assert asyncio.run(use_recovery_code(user_id, "not-a-code")) is False
    assert asyncio.run(count_recovery_codes(user_id)) == 9


def test_a_new_set_voids_the_old_one(account):
    user_id, _email, _central = account
    old = asyncio.run(replace_recovery_codes(user_id))
    asyncio.run(replace_recovery_codes(user_id))
    assert asyncio.run(use_recovery_code(user_id, old[0])) is False
    asyncio.run(delete_recovery_codes(user_id))
    assert asyncio.run(count_recovery_codes(user_id)) == 0


def test_deleting_the_account_removes_its_links_and_codes(account):
    user_id, email, central = account
    asyncio.run(create_sign_in_link(email, user_id=user_id))
    asyncio.run(replace_recovery_codes(user_id))
    central.execute("PRAGMA foreign_keys = ON")
    central.execute("DELETE FROM users WHERE id = ?", (user_id,))
    central.commit()
    assert central.execute("SELECT COUNT(*) FROM sign_in_links WHERE user_id = ?", (user_id,)).fetchone()[0] == 0
    assert central.execute("SELECT COUNT(*) FROM mfa_recovery_codes WHERE user_id = ?", (user_id,)).fetchone()[0] == 0
