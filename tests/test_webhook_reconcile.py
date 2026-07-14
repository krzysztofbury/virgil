"""Subscription reconcile: only THIS user's callbacks (and unowned orphans)
are torn down — other users on the same Oura OAuth app keep their sync."""

from app.services.oura_api import classify_subscription

BASE = "https://virgil.example.com"
OWN = {"a" * 32}
FOREIGN_ID = "b" * 32
ORPHAN_ID = "c" * 32
KNOWN = {"a" * 32, FOREIGN_ID}


def test_own_callback_deleted():
    assert classify_subscription(f"{BASE}/api/oura/webhook/{'a' * 32}", BASE, OWN, KNOWN) == "delete"


def test_legacy_endpoint_deleted():
    assert classify_subscription(f"{BASE}/api/oura/webhook", BASE, OWN, KNOWN) == "delete"
    assert classify_subscription(f"{BASE}/api/oura/webhook/", BASE, OWN, KNOWN) == "delete"


def test_orphan_unowned_id_deleted():
    assert classify_subscription(f"{BASE}/api/oura/webhook/{ORPHAN_ID}", BASE, OWN, KNOWN) == "delete"


def test_other_users_active_callback_preserved():
    """Users can share one Oura OAuth app — reconcile must never delete an
    active callback owned by someone else."""
    assert classify_subscription(f"{BASE}/api/oura/webhook/{FOREIGN_ID}", BASE, OWN, KNOWN) == "foreign"


def test_other_deployments_untouched():
    assert classify_subscription("https://other.example.com/api/oura/webhook/x", BASE, OWN, KNOWN) == "unrelated"
    assert classify_subscription(f"{BASE}/some/other/path", BASE, OWN, KNOWN) == "unrelated"
    assert classify_subscription(f"{BASE}/api/oura/webhookfoo", BASE, OWN, KNOWN) == "unrelated"
    assert classify_subscription("", BASE, OWN, KNOWN) == "unrelated"


def test_trailing_slash_in_base_url_tolerated():
    assert classify_subscription(f"{BASE}/api/oura/webhook/{'a' * 32}", BASE + "/", OWN, KNOWN) == "delete"
