"""Shared Monday-Sunday calendar boundaries for experiments."""

from datetime import date, timedelta


def experiment_calendar(start: date, num_weeks: int) -> tuple[date, date]:
    if num_weeks < 1:
        raise ValueError("num_weeks must be positive")
    first_monday = start - timedelta(days=start.weekday())
    final_sunday = first_monday + timedelta(weeks=num_weeks, days=-1)
    return first_monday, final_sunday


def experiment_week_number(start: date, num_weeks: int, reference: date) -> int:
    first_monday, final_sunday = experiment_calendar(start, num_weeks)
    bounded_reference = min(max(reference, start), final_sunday)
    reference_monday = bounded_reference - timedelta(days=bounded_reference.weekday())
    return ((reference_monday - first_monday).days // 7) + 1
