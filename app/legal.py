"""The version of the privacy policy and the terms (#179, #182).

One date for both documents: the day they take effect. An account stores the
version it accepted at signup, so a later change can be told apart. Change it
together with the documents in app/templates/legal/.
"""

from datetime import date

LEGAL_VERSION = "2026-10-28"

_MONTHS_EN = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def effective_date(language: str) -> str:
    """LEGAL_VERSION as each page writes it: 28.10.2026, or 28 October 2026."""
    day = date.fromisoformat(LEGAL_VERSION)
    if language == "pl":
        return day.strftime("%d.%m.%Y")
    return f"{day.day} {_MONTHS_EN[day.month - 1]} {day.year}"
