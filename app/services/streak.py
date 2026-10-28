from datetime import date, timedelta


async def break_dates(db, habit_id: int, start: str, end: str) -> tuple[set[str], set[str]]:
    """Relapse dates and near-miss dates for one habit, inclusive.

    A relapse is a slip. A near miss is not a relapse, and it is not a clean day
    either. Callers that want unclean days take the union, so a day that is both
    counts once.
    """
    relapses = {
        row["date"]
        for row in await db.execute_fetchall(
            "SELECT DISTINCT date FROM pmo_events "
            "WHERE habit_id = ? AND event_type = 'relapse' AND date BETWEEN ? AND ?",
            (habit_id, start, end),
        )
    }
    near = {
        row["date"]
        for row in await db.execute_fetchall(
            "SELECT DISTINCT date FROM feniks_daily WHERE habit_id = ? AND near_miss = 1 AND date BETWEEN ? AND ?",
            (habit_id, start, end),
        )
    }
    return relapses, near


async def get_week_clean(db, habit_id: int) -> tuple[int, int, int]:
    """One habit's current-week clean rate (Gola method: 75%/week target), Monday..today.

    clean = days elapsed this week minus unclean days this week; pct over elapsed.
    Unclean means a relapse or a near miss. A single unclean day drops the rate
    (e.g. 6/7 = 86%) but never hard-resets to 0.
    Returns (clean_days, days_elapsed, pct).
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    elapsed = (today - monday).days + 1  # 1..7 inclusive
    relapses, near = await break_dates(db, habit_id, monday.isoformat(), today.isoformat())
    clean = max(0, elapsed - len(relapses | near))
    pct = round(clean / elapsed * 100) if elapsed else 0
    return clean, elapsed, pct


async def get_streak(db, habit_id: int) -> tuple[int, date | None]:
    """Return (streak_days, last_relapse_date) for one habit.

    streak_days is the days since the later of the last relapse and the last
    near miss. A near miss breaks the streak and is not a relapse, so
    last_relapse_date stays the last relapse marker, or None when the person
    has not slipped. Do not recompute the streak from last_relapse_date: an
    edging day after the last slip makes them disagree on purpose.
    Without either, the streak is the days since the habit's start date.
    """
    today = date.today()
    today_iso = today.isoformat()

    rows = await db.execute_fetchall(
        """
        SELECT MAX(date) AS break_date FROM (
            SELECT date FROM pmo_events
            WHERE habit_id = ? AND event_type = 'relapse' AND date <= ?
            UNION
            SELECT date FROM feniks_daily
            WHERE habit_id = ? AND near_miss = 1 AND date <= ?
        )
        """,
        (habit_id, today_iso, habit_id, today_iso),
    )
    last = await db.execute_fetchall(
        """
        SELECT MAX(date) AS relapse_date FROM pmo_events
        WHERE habit_id = ? AND event_type = 'relapse' AND date <= ?
        """,
        (habit_id, today_iso),
    )
    relapse_iso = last[0]["relapse_date"] if last else None
    relapse_date = date.fromisoformat(relapse_iso) if relapse_iso else None
    break_iso = rows[0]["break_date"] if rows else None
    if break_iso:
        return (today - date.fromisoformat(break_iso)).days, relapse_date

    start = await db.execute_fetchall("SELECT start_date FROM habits WHERE id = ?", (habit_id,))
    if start:
        return max(0, (today - date.fromisoformat(start[0]["start_date"])).days), None

    return 0, None
