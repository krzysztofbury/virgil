"""Morning briefing scheduling: once per day, not before dawn, backoff on failure."""

from datetime import datetime, timedelta

from app.services.scheduler import _briefing_due


def test_not_before_earliest_hour():
    early = datetime(2026, 7, 13, 5, 59)
    assert _briefing_due(early, last_day="", last_attempt="") is False


def test_due_in_the_morning_with_no_history():
    morning = datetime(2026, 7, 13, 6, 30)
    assert _briefing_due(morning, last_day="", last_attempt="") is True


def test_not_due_twice_same_day():
    morning = datetime(2026, 7, 13, 9, 0)
    assert _briefing_due(morning, last_day="2026-07-13", last_attempt="") is False


def test_due_again_next_day():
    morning = datetime(2026, 7, 14, 7, 0)
    assert _briefing_due(morning, last_day="2026-07-13", last_attempt="") is True


def test_failure_backoff_within_an_hour():
    now = datetime(2026, 7, 13, 8, 0)
    recent_attempt = (datetime.now().astimezone() - timedelta(minutes=10)).isoformat()
    assert _briefing_due(now, last_day="", last_attempt=recent_attempt) is False

    old_attempt = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
    assert _briefing_due(now, last_day="", last_attempt=old_attempt) is True
