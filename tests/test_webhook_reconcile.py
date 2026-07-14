"""Subscription reconcile: only THIS deployment's callbacks are torn down."""

from app.services.oura_api import _subscription_points_at

BASE = "https://virgil.example.com"


def test_matches_per_user_and_legacy_callbacks():
    assert _subscription_points_at({"callback_url": f"{BASE}/api/oura/webhook/abc123"}, BASE)
    assert _subscription_points_at({"callback_url": f"{BASE}/api/oura/webhook"}, BASE)


def test_ignores_foreign_callbacks():
    assert not _subscription_points_at({"callback_url": "https://other.example.com/api/oura/webhook/abc"}, BASE)
    assert not _subscription_points_at({"callback_url": f"{BASE}/some/other/path"}, BASE)
    assert not _subscription_points_at({}, BASE)


def test_trailing_slash_in_base_url_tolerated():
    assert _subscription_points_at({"callback_url": f"{BASE}/api/oura/webhook/abc"}, BASE + "/")
