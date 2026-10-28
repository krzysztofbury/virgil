"""Email addresses typed into signup and the admin invite form."""

import time

import pytest

from app.validation import valid_email


@pytest.mark.parametrize("value", ["a@b.co", "first.last+tag@mail.example.pl", "x@sub.domain.org"])
def test_ordinary_addresses_are_valid(value):
    assert valid_email(value)


@pytest.mark.parametrize(
    "value", ["", "plain", "a@b", "a@.b", "a@b.", "a b@c.d", "a@b@c.d", "@b.co", "a@", "a@b.c " + "x"]
)
def test_malformed_addresses_are_refused(value):
    assert not valid_email(value)


def test_an_address_longer_than_rfc_5321_allows_is_refused():
    assert not valid_email("a@" + "b" * 250 + ".com")


def test_a_hostile_address_is_refused_quickly():
    """The old pattern backtracked quadratically on a run of dots, so one signup
    POST could hold a worker for minutes."""
    started = time.perf_counter()
    assert not valid_email("a@" + "." * 50_000 + "@")
    assert time.perf_counter() - started < 0.5


def test_signup_refuses_a_hostile_address_quickly(auth_client, monkeypatch):
    from conftest import csrf_token


    monkeypatch.setattr("app.config.REGISTRATION_OPEN", True)
    token = csrf_token(auth_client, "/login")
    started = time.perf_counter()
    resp = auth_client.post(
        "/signup",
        data={
            "email": "a@" + "." * 50_000 + "@",
            "password": "password-123",  # pragma: allowlist secret - synthetic fixture
            "password_confirm": "password-123",  # pragma: allowlist secret - synthetic fixture
            "_csrf_token": token,
        },
        follow_redirects=False,
    )
    assert time.perf_counter() - started < 2
    assert "Please enter a valid email address." in resp.text
