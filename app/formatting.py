"""Display helpers for values whose stored unit is not the unit a human reads.

`training_entries.duration` is stored in seconds — that is what the WOD parser
produces and what DURATION_SECONDS_MAX validates. The training page used to
print it with a literal " min" suffix, so a 69-minute bike ride logged as 4140
rendered as "4140.0 min" while the same session's header (from
`training_sessions.duration_minutes`, typed by hand) correctly said "69 min".
"""

from app.i18n import _, ngettext


def format_duration_seconds(value) -> str:
    """Render a duration stored in seconds.

    Under a minute stays in seconds; whole minutes print as minutes; anything in
    between carries both parts. Minutes rather than hours above 60 so this agrees
    with the session header, which is in minutes — "69 min" in both places rather
    than "69 min" beside "1 h 9 min".

    Returns "" for a value that is absent or not a number, so a caller can fall
    back to its own placeholder rather than printing "None".
    """
    try:
        total = float(value)
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""

    seconds = int(round(total))
    if seconds < 60:
        return f"{seconds} s"

    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min" if rest == 0 else f"{minutes} min {rest} s"


def format_thousands(value) -> str:
    """Group the digits of a count, 11089 -> "11,089". Returns "" when absent."""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return ""


def _plain_number(value: float) -> str:
    """300.0 -> "300", 5.6 -> "5.6": a reference bound needs no trailing zero."""
    return f"{value:g}"


def format_ref_range(low, high) -> str:
    """Render a lab reference range from its optional bounds.

    Both bounds read "30-100"; one bound reads "< 100" or "> 30". A bound of 0 is
    a real bound, not a missing one, which is why this tests for None rather than
    truthiness. Returns "" when neither bound is set.
    """
    if low is not None and high is not None:
        return f"{_plain_number(low)}-{_plain_number(high)}"
    if high is not None:
        return f"< {_plain_number(high)}"
    if low is not None:
        return f"> {_plain_number(low)}"
    return ""


def time_ago(then, now) -> str:
    """How long ago `then` was, in the largest whole unit: "3 hours ago"."""
    minutes = max(0, int((now - then).total_seconds() // 60))
    if minutes >= 1440:
        return ngettext("{num} day ago", "{num} days ago", minutes // 1440).format(num=minutes // 1440)
    if minutes >= 60:
        return ngettext("{num} hour ago", "{num} hours ago", minutes // 60).format(num=minutes // 60)
    if minutes >= 1:
        return ngettext("{num} minute ago", "{num} minutes ago", minutes).format(num=minutes)
    return _("just now")
