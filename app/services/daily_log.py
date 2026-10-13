"""Daily log (habits, energy, A.N.D.Y.) writes shared by the form and REST.

The form always posts every field, so it replaces the day. An API client
usually knows only part of the day ("body done, energy 6"), so the write is a
merge: fields absent from `changes` keep their stored value, and a day that
does not exist yet starts from the schema defaults.
"""

STATUSES = ("done", "skipped", "pending")
STATUS_FIELDS = (
    "morning_routine",
    "evening_routine",
    "water",
    "andy_body_status",
    "andy_spirit_status",
    "andy_account_status",
    "andy_relations_status",
)
DESC_FIELDS = ("andy_body_desc", "andy_spirit_desc", "andy_account_desc", "andy_relations_desc")
DESC_MAX = 500
NOTES_MAX = 5000
FIELDS = ("energy", *STATUS_FIELDS, *DESC_FIELDS, "notes")

_DEFAULTS = {"energy": None, **dict.fromkeys(STATUS_FIELDS, "pending"), **dict.fromkeys(DESC_FIELDS, ""), "notes": ""}


class DailyLogError(ValueError):
    pass


def _validate(changes: dict) -> dict:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise DailyLogError(f"Unknown daily-log fields: {', '.join(sorted(unknown))}")
    clean: dict = {}
    for field, value in changes.items():
        if field == "energy":
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10):
                raise DailyLogError("energy must be an integer from 1 to 10, or null")
        elif field in STATUS_FIELDS:
            if value not in STATUSES:
                raise DailyLogError(f"{field} must be one of: {', '.join(STATUSES)}")
        else:
            limit = NOTES_MAX if field == "notes" else DESC_MAX
            if not isinstance(value, str):
                raise DailyLogError(f"{field} must be text")
            if len(value) > limit:
                raise DailyLogError(f"{field} must be at most {limit} characters")
        clean[field] = value
    return clean


async def upsert_daily_log(db, day: str, changes: dict) -> dict:
    """Merge `changes` into the log for ISO date `day` and return the stored row."""
    clean = _validate(changes)
    if db.in_transaction:
        raise RuntimeError("Daily-log writes require a connection without pending writes")
    await db.execute("BEGIN IMMEDIATE")
    try:
        rows = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (day,))
        current = {field: rows[0][field] for field in FIELDS} if rows else dict(_DEFAULTS)
        current.update(clean)
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
