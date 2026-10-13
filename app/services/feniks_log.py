"""Feniks day-log and brick writes shared by the form and REST.

A watched day also records one relapse pmo_event for that date; correcting the
day back to clean removes only that marker ('via daily log'), never a relapse
recorded by another path, so the streak and the weekly clean rate stay
consistent with the day log.
"""

from datetime import date as calendar_date

WATCHED_MIN_MINUTES = 5
MINUTES_MAX = 1440
NOTE_MAX = 500
HOOK_MAX = 200
STORY_MAX = 2000
DAILY_MARKER = "via daily log"


class FeniksWriteError(ValueError):
    pass


def past_or_today(value: str) -> str | None:
    """Normalise an ISO date, or None if it is unparseable or still ahead.

    Normalising matters because date.fromisoformat also accepts compact and
    week forms, which no longer compare correctly as plain text.
    """
    try:
        parsed = calendar_date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.isoformat() if parsed <= calendar_date.today() else None


async def save_day(db, day: str, *, used: bool, minutes: int | None, edging: bool, note: str) -> dict:
    """Upsert the day outcome. Watched means at least five total minutes."""
    entry_date = past_or_today(day)
    if entry_date is None:
        raise FeniksWriteError("Choose a date of today or earlier.")
    if minutes is not None:
        minutes = max(0, min(MINUTES_MAX, minutes))
    if used and (minutes is None or minutes < WATCHED_MIN_MINUTES):
        raise FeniksWriteError(
            "Watched means at least 5 total minutes. Record a shorter exposure as clean and add it to the note."
        )
    if not used:
        minutes = None
        edging = False
    note = (note or "")[:NOTE_MAX]
    if db.in_transaction:
        raise RuntimeError("Feniks writes require a connection without pending writes")
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            """
            INSERT INTO feniks_daily (date, used, minutes, edging, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                used=excluded.used, minutes=excluded.minutes,
                edging=excluded.edging, note=excluded.note
            """,
            (entry_date, int(used), minutes, int(edging), note),
        )
        if used:
            await db.execute(
                """
                INSERT INTO pmo_events (date, event_type, notes)
                SELECT ?, 'relapse', ?
                WHERE NOT EXISTS (SELECT 1 FROM pmo_events WHERE date = ? AND event_type = 'relapse')
                """,
                (entry_date, DAILY_MARKER, entry_date),
            )
        else:
            await db.execute(
                "DELETE FROM pmo_events WHERE date = ? AND event_type = 'relapse' AND notes = ?",
                (entry_date, DAILY_MARKER),
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return {"date": entry_date, "used": int(used), "minutes": minutes, "edging": int(edging), "note": note}


async def add_brick(db, day: str, *, hook: str, craving: int | None, story: str) -> dict:
    """A brick is one urge survived. The hook is what makes it retrievable, so it is required."""
    hook = (hook or "").strip()
    if not hook:
        raise FeniksWriteError("Name the brick before saving it.")
    entry_date = past_or_today(day)
    if entry_date is None:
        raise FeniksWriteError("Give the brick a date of today or earlier.")
    if craving is not None:
        craving = max(0, min(10, craving))
    hook = hook[:HOOK_MAX]
    story = (story or "")[:STORY_MAX]
    cursor = await db.execute(
        "INSERT INTO feniks_bricks (date, hook, craving, story) VALUES (?, ?, ?, ?)",
        (entry_date, hook, craving, story),
    )
    await db.commit()
    return {"id": cursor.lastrowid, "date": entry_date, "hook": hook, "craving": craving, "story": story}
