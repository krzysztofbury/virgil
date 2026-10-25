"""Daily log (habits, energy, A.N.D.Y.) writes shared by the form and REST.

The form always posts every field, so it replaces the day. An API client
usually knows only part of the day ("body done, energy 6"), so the write is a
merge: fields absent from `changes` keep their stored value, and a day that
does not exist yet starts from the schema defaults.
"""

from datetime import date, timedelta

from app.i18n import N_, LocalizedError, month_short

STATUSES = ("done", "skipped", "pending")
NEXT_STATUS = {"pending": "done", "done": "skipped", "skipped": "pending"}
STATUS_FIELDS = (
    "morning_routine",
    "evening_routine",
    "water",
    "andy_body_status",
    "andy_spirit_status",
    "andy_account_status",
    "andy_relations_status",
)
ROUTINE_FIELDS = STATUS_FIELDS[:3]
ANDY_STATUS_FIELDS = STATUS_FIELDS[3:]
DESC_FIELDS = ("andy_body_desc", "andy_spirit_desc", "andy_account_desc", "andy_relations_desc")
DESC_MAX = 500
NOTES_MAX = 5000
FIELDS = ("energy", *STATUS_FIELDS, *DESC_FIELDS, "notes")

_DEFAULTS = {"energy": None, **dict.fromkeys(STATUS_FIELDS, "pending"), **dict.fromkeys(DESC_FIELDS, ""), "notes": ""}


class DailyLogError(LocalizedError):
    pass


def _validate(changes: dict) -> dict:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise DailyLogError(N_("Unknown daily-log fields: {fields}"), fields=", ".join(sorted(unknown)))
    clean: dict = {}
    for field, value in changes.items():
        if field == "energy":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10):
                raise DailyLogError(N_("energy must be an integer from 1 to 10, or null"))
        elif field in STATUS_FIELDS:
            if value not in STATUSES:
                raise DailyLogError(N_("{field} must be one of: {choices}"), field=field, choices=", ".join(STATUSES))
        else:
            limit = NOTES_MAX if field == "notes" else DESC_MAX
            if not isinstance(value, str):
                raise DailyLogError(N_("{field} must be text"), field=field)
            if len(value) > limit:
                raise DailyLogError(N_("{field} must be at most {limit} characters"), field=field, limit=limit)
        clean[field] = value
    return clean


async def upsert_daily_log(db, day: str, changes: dict) -> dict:
    """Merge `changes` into the log for ISO date `day` and return the stored row."""
    clean = _validate(changes)
    return await _rewrite(db, day, lambda current: current.update(clean))


async def cycle_status(db, day: str, field: str) -> dict:
    """Advance one status a step and return the stored row.

    Pending, done, skipped, then pending again: the order the daily page cycles
    in. The read and the write share one transaction, so two quick taps advance
    twice instead of both reading the same state.
    """
    if field not in STATUS_FIELDS:
        raise DailyLogError(N_("{field} is not a daily-log status"), field=field)

    def advance(current: dict) -> None:
        current[field] = NEXT_STATUS.get(current[field], "done")

    return await _rewrite(db, day, advance)


async def _rewrite(db, day: str, apply) -> dict:
    """Load the day (or the defaults), let `apply` change it in place, store it."""
    if db.in_transaction:
        raise RuntimeError("Daily-log writes require a connection without pending writes")
    await db.execute("BEGIN IMMEDIATE")
    try:
        rows = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (day,))
        current = {field: rows[0][field] for field in FIELDS} if rows else dict(_DEFAULTS)
        apply(current)
        columns = ", ".join(FIELDS)
        placeholders = ", ".join("?" * len(FIELDS))
        assignments = ", ".join(f"{field}=excluded.{field}" for field in FIELDS)
        await db.execute(
            f"INSERT INTO daily_logs (date, {columns}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(date) DO UPDATE SET {assignments}, updated_at=datetime('now')",
            (day, *(current[field] for field in FIELDS)),
        )
        stored = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (day,))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return dict(stored[0])


def day_progress(log: dict | None) -> dict:
    """How much of a day is done: A.N.D.Y. tasks and routines, counted separately."""

    def done(fields) -> int:
        return sum(1 for field in fields if log and log[field] == "done")

    return {
        "andy_done": done(ANDY_STATUS_FIELDS),
        "andy_total": len(ANDY_STATUS_FIELDS),
        "routines_done": done(ROUTINE_FIELDS),
        "routines_total": len(ROUTINE_FIELDS),
    }


async def completion_weeks(db, last: date, weeks: int) -> list[dict]:
    """Habits done per day for the `weeks` Monday-to-Sunday weeks ending with the one holding `last`.

    A day without a log has `done` None, so "not logged" stays distinct from
    "logged, nothing done". Days after `last` are `future` and count toward no
    average; a week's average is over its days up to `last`, unlogged ones as 0.
    """
    first = last - timedelta(days=last.weekday() + 7 * (weeks - 1))
    rows = await db.execute_fetchall(
        f"SELECT date, {', '.join(STATUS_FIELDS)} FROM daily_logs WHERE date BETWEEN ? AND ?",  # noqa: S608
        (first.isoformat(), last.isoformat()),
    )
    done_by_day = {row["date"]: sum(1 for field in STATUS_FIELDS if row[field] == "done") for row in rows}
    result = []
    for index in range(weeks):
        start = first + timedelta(weeks=index)
        days = [start + timedelta(days=offset) for offset in range(7)]
        elapsed = [day for day in days if day <= last]
        done_total = sum(done_by_day.get(day.isoformat(), 0) for day in elapsed)
        result.append(
            {
                "start": start.isoformat(),
                # Label a column only where a month starts, as a calendar does.
                "month": month_short(start.month) if index == 0 or start.day <= 7 else "",
                "days": [
                    {"date": day.isoformat(), "done": done_by_day.get(day.isoformat()), "future": day > last}
                    for day in days
                ],
                "average": round(done_total / (len(elapsed) * len(STATUS_FIELDS)) * 100),
            }
        )
    return result


async def get_daily_log(db, day: str) -> dict | None:
    rows = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (day,))
    return dict(rows[0]) if rows else None


async def delete_daily_log(db, day: str) -> bool:
    cursor = await db.execute("DELETE FROM daily_logs WHERE date = ?", (day,))
    await db.commit()
    return cursor.rowcount > 0
