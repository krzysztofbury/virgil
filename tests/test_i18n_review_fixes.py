"""Regressions from the #152 review: user text in error messages, and labels."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from conftest import _TMP, TEST_EMAIL

from app.i18n import N_, reset_language, set_language


@pytest.fixture
def token(auth_client):
    from app.central_db import create_api_token

    central = sqlite3.connect(Path(_TMP) / "central.db")
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.close()
    secret = asyncio.run(create_api_token(user_id, "review", ["read", "write", "finance"], 1))
    try:
        yield {"Authorization": f"Bearer {secret}"}
    finally:
        central = sqlite3.connect(Path(_TMP) / "central.db")
        central.execute("DELETE FROM api_tokens WHERE name = 'review'")
        central.commit()
        central.close()


def test_braces_in_a_library_alias_are_refused_not_a_crash(auth_client, token):
    response = auth_client.post(
        "/api/library",
        headers=token,
        json={"name": "Brace probe", "section": "Core", "aliases": ["{x}" + "a" * 90]},
    )
    assert response.status_code == 422
    assert response.json()["detail"].startswith("alias '{x}")


@pytest.mark.parametrize("text", ["{oops}", "{}", "{0}:5"])
def test_braces_in_finance_thresholds_are_refused_not_a_crash(text):
    from app.routers.finance import _parse_thresholds
    from app.services.finance import FinanceError

    with pytest.raises(FinanceError):
        _parse_thresholds("months", text)


def test_only_labelled_values_are_translated():
    """A user's own name, or an empty value, is shown as given."""
    from app.services.data_transfer import ImportRefused
    from app.services.finance import FinanceError

    token = set_language("pl")
    try:
        assert FinanceError(N_("{value} is listed twice"), value="Cash").localized() == "Cash występuje dwa razy"
        refused = ImportRefused(
            N_("This export uses format version {value!r}, not {FORMAT_VERSION}."), value="", FORMAT_VERSION=1
        )
        assert "Project-Id" not in refused.localized()
        assert refused.localized() == "Ten eksport ma format w wersji '', a nie 1."
    finally:
        reset_language(token)


def test_wod_confirm_errors_name_the_field_in_polish():
    from app.routers.training import _ConfirmRejected

    token = set_language("pl")
    try:
        error = _ConfirmRejected(N_('row {row}: {field} "{raw}" is not a whole number'), row=2, field="set", raw="x")
        assert error.localized() == 'wiersz 2: Seria "x" nie jest liczbą całkowitą'
        error = _ConfirmRejected(N_('row {row}: {field} "{raw}" is not a number'), row=1, field="time", raw="x")
        assert error.localized() == 'wiersz 1: Czas "x" nie jest liczbą'
    finally:
        reset_language(token)
