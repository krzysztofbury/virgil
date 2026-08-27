"""The weekly training schedule the A.N.D.Y. planner reasons about.

This replaces the per-exercise "training protocol" block that used to be fed to
the planner. That block listed prescriptions (`- Goblet Squat (KB): 4x10-12`)
read from `training_exercises`, and it stopped describing reality the moment
training moved from a basement kettlebell program to a CrossFit box: the rows
stayed, the planner kept reading them, and nothing in the UI made the staleness
visible.

A schedule is the smaller and truer thing. The planner needs to know whether
today is a training day and whether the week is on track — not which movements
to prescribe, because the box programs the session.

Stored in `app_settings` rather than in code because this value changes: it went
from Mon/Tue/Wed to Mon/Wed/Fri inside a week of first being written down. A
deploy per revision is the wrong cost for something that volatile, and this box
auto-pulls images unattended.
"""

from datetime import date, timedelta

from app.db import get_setting

# Indexed by date.weekday(), so DAY_KEYS[d.weekday()] is d's own key.
DAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_SHORT = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_FULL = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

SETTING_DAYS = "training_days"
SETTING_SWIM = "training_swim_per_week"
# Empty on purpose. A default of "mon,wed,fri" made every new user look like they
# had chosen a CrossFit week, and the planner then reasoned about a schedule
# nobody had set. normalize_days("") already renders the honest sentence.
DEFAULT_DAYS = ""
DEFAULT_SWIM = "0"
SWIM_PER_WEEK_MAX = 7


def normalize_days(raw: str) -> list[str]:
    """Parse a comma-separated day list into canonical weekday order.

    Accepts `mon`, `Monday`, ` MON ` alike. Unknown tokens and duplicates are
    dropped rather than raising: this value ends up in an LLM prompt, and one
    typo in one field should not cost the planner its entire schedule block.
    """
    seen = {token.strip().lower()[:3] for token in raw.split(",") if token.strip()}
    return [day for day in DAY_KEYS if day in seen]


def parse_swim_per_week(raw: str) -> int:
    """Weekly swim target. Unset, negative or unparseable all mean 0 (no target)."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return min(max(value, 0), SWIM_PER_WEEK_MAX)


def format_days(days: list[str]) -> str:
    return ", ".join(DAY_SHORT[DAY_KEYS.index(day)] for day in days)


def _session_label(row) -> str:
    duration = row["duration_minutes"]
    return f"{row['date']} ({duration} min)" if duration else str(row["date"])


async def schedule_block(db, target: date) -> str:
    """Build the planner's training context for `target`.

    Three facts, in the order the planner needs them: what the week is supposed
    to look like, whether today is on it, and what has actually been logged.
    """
    days = normalize_days(await get_setting(db, SETTING_DAYS, DEFAULT_DAYS))
    swim = parse_swim_per_week(await get_setting(db, SETTING_SWIM, DEFAULT_SWIM))

    # Sport-neutral wording. The schedule is a set of days, not a program: the
    # box decides what the session is, and the sport can change without this
    # block having to be edited around it.
    plan = f"Training days: {format_days(days)}." if days else "No fixed training days set."
    if swim:
        plan += f" Swimming: {swim}x/week, any day."
    plan += " Everything else is optional."

    weekday = target.weekday()
    on_plan = "a scheduled training day" if DAY_KEYS[weekday] in days else "not a scheduled training day"
    lines = ["--- Training plan ---", plan, f"Today is {DAY_FULL[weekday]} - {on_plan}."]

    # Bounded at both ends: the planner can be run for a past date, and a
    # session logged after `target` is not something that day could know about.
    monday = target - timedelta(days=weekday)
    week_rows = await db.execute_fetchall(
        "SELECT date, duration_minutes FROM training_sessions WHERE date >= ? AND date <= ? ORDER BY date",
        (monday.isoformat(), target.isoformat()),
    )
    if week_rows:
        lines.append("Logged this week (since Monday): " + ", ".join(_session_label(r) for r in week_rows) + ".")
    else:
        lines.append("Logged this week (since Monday): nothing yet.")
        # Only worth naming the previous session when the week is still empty —
        # otherwise it is noise the planner has to reason past.
        last_rows = await db.execute_fetchall(
            "SELECT date, duration_minutes FROM training_sessions WHERE date < ? ORDER BY date DESC LIMIT 1",
            (monday.isoformat(),),
        )
        if last_rows:
            lines.append(f"Last session before this week: {_session_label(last_rows[0])}.")

    return "\n".join(lines)
