"""Freedom day-log and brick writes, per habit, shared by the forms and REST.

A slipped day also records one relapse marker (pmo_events) for that habit and
date; correcting the day back to clean removes only that marker ('via daily
log'), never a relapse recorded another way, so the streak and the weekly clean
rate stay consistent with the day log.

Whether a day counts as a slip is the habit's own definition: with an amount
unit, a slip needs at least `slip_threshold` of it (porn: 5 minutes), and less
stays a clean day with the detail in the note.
"""

from datetime import date as calendar_date

AMOUNT_MAX = 1440
NOTE_MAX = 500
HOOK_MAX = 200
STORY_MAX = 2000
DAILY_MARKER = "via daily log"


class FreedomWriteError(ValueError):
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


def _require_idle(db) -> None:
    if db.in_transaction:
        raise RuntimeError("Freedom writes require a connection without pending writes")


async def save_day(db, habit: dict, day: str, *, slipped: bool, amount: int | None, near_miss: bool, note: str) -> dict:
    """Upsert one habit's outcome for one day."""
    entry_date = past_or_today(day)
    if entry_date is None:
        raise FreedomWriteError("Choose a date of today or earlier.")
    unit, threshold = habit["amount_unit"], habit["slip_threshold"]
    if not unit:
        amount = None
    elif amount is not None:
        amount = max(0, min(AMOUNT_MAX, amount))
    if slipped and unit and threshold and (amount is None or amount < threshold):
        raise FreedomWriteError(
            f"A slip means at least {threshold} {unit}. Record less as a clean day and add it to the note."
        )
    if not slipped:
        amount = None
        near_miss = False
    if not habit["near_miss_label"]:
        near_miss = False
    note = (note or "")[:NOTE_MAX]
    _require_idle(db)
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute(
            """
            INSERT INTO feniks_daily (habit_id, date, slipped, amount, near_miss, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(habit_id, date) DO UPDATE SET
                slipped=excluded.slipped, amount=excluded.amount,
                near_miss=excluded.near_miss, note=excluded.note
            """,
            (habit["id"], entry_date, int(slipped), amount, int(near_miss), note),
        )
        if slipped:
            await db.execute(
                """
                INSERT INTO pmo_events (habit_id, date, event_type, notes)
                SELECT ?, ?, 'relapse', ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM pmo_events WHERE habit_id = ? AND date = ? AND event_type = 'relapse'
                )
                """,
                (habit["id"], entry_date, DAILY_MARKER, habit["id"], entry_date),
            )
        else:
            await db.execute(
                "DELETE FROM pmo_events WHERE habit_id = ? AND date = ? AND event_type = 'relapse' AND notes = ?",
                (habit["id"], entry_date, DAILY_MARKER),
            )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return {
        "habit": habit["slug"],
        "date": entry_date,
        "slipped": int(slipped),
        "amount": amount,
        "near_miss": int(near_miss),
        "note": note,
    }


async def delete_day(db, habit: dict, day: str) -> None:
    """Remove one habit's day entry and the relapse marker it created, nothing else."""
    _require_idle(db)
    await db.execute("BEGIN IMMEDIATE")
    try:
        cursor = await db.execute("DELETE FROM feniks_daily WHERE habit_id = ? AND date = ?", (habit["id"], day))
        if cursor.rowcount == 0:
            raise LookupError("No day entry for that date.")
        await db.execute(
            "DELETE FROM pmo_events WHERE habit_id = ? AND date = ? AND event_type = 'relapse' AND notes = ?",
            (habit["id"], day, DAILY_MARKER),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def add_brick(db, habit: dict, day: str, *, hook: str, craving: int | None, story: str) -> dict:
    """A brick is one urge survived. The hook is what makes it retrievable, so it is required."""
    hook = (hook or "").strip()
    if not hook:
        raise FreedomWriteError("Name the brick before saving it.")
    entry_date = past_or_today(day)
    if entry_date is None:
        raise FreedomWriteError("Give the brick a date of today or earlier.")
    if craving is not None:
        craving = max(0, min(10, craving))
    hook = hook[:HOOK_MAX]
    story = (story or "")[:STORY_MAX]
    cursor = await db.execute(
        "INSERT INTO feniks_bricks (habit_id, date, hook, craving, story) VALUES (?, ?, ?, ?, ?)",
        (habit["id"], entry_date, hook, craving, story),
    )
    await db.commit()
    return {
        "id": cursor.lastrowid,
        "habit": habit["slug"],
        "date": entry_date,
        "hook": hook,
        "craving": craving,
        "story": story,
    }


async def update_brick(db, brick_id: int, changes: dict) -> dict:
    """Change only the supplied brick fields; the hook cannot become blank."""
    rows = await db.execute_fetchall(
        "SELECT b.*, h.slug AS habit FROM feniks_bricks b JOIN habits h ON h.id = b.habit_id WHERE b.id = ?",
        (brick_id,),
    )
    if not rows:
        raise LookupError("Brick was not found.")
    brick = dict(rows[0])
    if "date" in changes:
        entry_date = past_or_today(changes["date"])
        if entry_date is None:
            raise FreedomWriteError("Give the brick a date of today or earlier.")
        brick["date"] = entry_date
    if "hook" in changes:
        hook = (changes["hook"] or "").strip()
        if not hook:
            raise FreedomWriteError("Name the brick before saving it.")
        brick["hook"] = hook[:HOOK_MAX]
    if "craving" in changes:
        craving = changes["craving"]
        brick["craving"] = None if craving is None else max(0, min(10, craving))
    if "story" in changes:
        brick["story"] = (changes["story"] or "")[:STORY_MAX]
    await db.execute(
        "UPDATE feniks_bricks SET date = ?, hook = ?, craving = ?, story = ? WHERE id = ?",
        (brick["date"], brick["hook"], brick["craving"], brick["story"], brick_id),
    )
    await db.commit()
    return {key: brick[key] for key in ("id", "habit", "date", "hook", "craving", "story")}


async def delete_brick(db, brick_id: int) -> dict:
    """Remove one brick; returns its date and habit, for the page to return to."""
    rows = await db.execute_fetchall(
        "SELECT b.date, h.slug AS habit FROM feniks_bricks b JOIN habits h ON h.id = b.habit_id WHERE b.id = ?",
        (brick_id,),
    )
    if not rows:
        raise LookupError("Brick was not found.")
    await db.execute("DELETE FROM feniks_bricks WHERE id = ?", (brick_id,))
    await db.commit()
    return dict(rows[0])


async def habit_overview(db, habit: dict) -> dict:
    """Where one habit stands today: streak, this week, today's entry, urges survived."""
    from app.services.streak import get_streak, get_week_clean

    streak_days, last_relapse = await get_streak(db, habit["id"])
    clean, elapsed, pct = await get_week_clean(db, habit["id"])
    today = calendar_date.today().isoformat()
    entry = await db.execute_fetchall(
        "SELECT date, slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?",
        (habit["id"], today),
    )
    bricks = await db.execute_fetchall("SELECT COUNT(*) AS c FROM feniks_bricks WHERE habit_id = ?", (habit["id"],))
    return {
        "habit": habit["slug"],
        "name": habit["name"],
        "streak_days": streak_days,
        "last_relapse": last_relapse.isoformat() if last_relapse else None,
        "week_clean": {"clean_days": clean, "days_elapsed": elapsed, "pct": pct},
        "today": dict(entry[0]) if entry else None,
        "bricks_total": bricks[0]["c"],
    }
