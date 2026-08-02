"""Display helpers for values whose stored unit is not the unit a human reads.

`training_entries.duration` is stored in seconds — that is what the WOD parser
produces and what DURATION_SECONDS_MAX validates. The training page used to
print it with a literal " min" suffix, so a 69-minute bike ride logged as 4140
rendered as "4140.0 min" while the same session's header (from
`training_sessions.duration_minutes`, typed by hand) correctly said "69 min".
"""


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
