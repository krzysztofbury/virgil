"""The one set of history windows every insight offers: 4, 12 and 26 weeks.

Each page used to pick its own (10 days of Oura trends, 30 Oura rows, 12 weeks
of personal bests, the latest two life scores), so the same question had a
different answer on every page. A page reads `?weeks=` and renders the shared
picker; the rest of the query string is kept, except what describes one moment
rather than the view: feedback, a job and a pagination cursor.
"""

from datetime import date, timedelta
from urllib.parse import urlencode

RANGE_WEEKS = (4, 12, 26)
DEFAULT_RANGE_WEEKS = 12
_TRANSIENT_PARAMS = frozenset({"msg", "err", "clear_draft", "job_id"})


def range_weeks(value) -> int:
    """The requested window, or the default for anything that is not one of the offered ones."""
    try:
        weeks = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RANGE_WEEKS
    return weeks if weeks in RANGE_WEEKS else DEFAULT_RANGE_WEEKS


def range_start(weeks: int, today: date) -> date:
    """The first day inside the window that ends today."""
    return today - timedelta(weeks=weeks) + timedelta(days=1)


def range_context(request, anchor: str, drop: tuple[str, ...] = ()) -> dict:
    """What the range picker partial needs: the current window and a link for each option.

    `drop` names the page's own query parameters that a new window must not carry.
    """
    weeks = range_weeks(request.query_params.get("weeks"))
    kept = {key: value for key, value in request.query_params.items() if key not in _TRANSIENT_PARAMS | set(drop)}
    links = []
    for option in RANGE_WEEKS:
        params = {**kept, "weeks": str(option)}
        links.append(
            {"weeks": option, "href": f"{request.url.path}?{urlencode(params)}#{anchor}", "active": option == weeks}
        )
    return {"range_weeks": weeks, "range_links": links}
