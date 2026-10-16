from datetime import date, timedelta


async def get_week_clean(db, habit_id: int) -> tuple[int, int, int]:
    """One habit's current-week clean rate (Gola method: 75%/week target), Monday..today.

    clean = days elapsed this week minus relapse days this week; pct over elapsed.
    A single slip drops the rate (e.g. 6/7 = 86%) but never hard-resets to 0.
    Returns (clean_days, days_elapsed, pct).
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    elapsed = (today - monday).days + 1  # 1..7 inclusive
    rows = await db.execute_fetchall(
        "SELECT COUNT(DISTINCT date) AS c FROM pmo_events "
        "WHERE habit_id = ? AND event_type = 'relapse' AND date BETWEEN ? AND ?",
        (habit_id, monday.isoformat(), today.isoformat()),
    )
    relapses = rows[0]["c"] if rows else 0
    clean = max(0, elapsed - relapses)
    pct = round(clean / elapsed * 100) if elapsed else 0
    return clean, elapsed, pct


async def get_streak(db, habit_id: int) -> tuple[int, date | None]:
    """Return (streak_days, last_relapse_date) for one habit.
    Streak = days since its last relapse; without one, days since its start date.
    """
    today = date.today()

    last_relapse = await db.execute_fetchall(
        "SELECT date FROM pmo_events WHERE habit_id = ? AND event_type = 'relapse' ORDER BY date DESC LIMIT 1",
        (habit_id,),
    )
    if last_relapse:
        relapse_date = date.fromisoformat(last_relapse[0]["date"])
        return (today - relapse_date).days, relapse_date

    start = await db.execute_fetchall("SELECT start_date FROM habits WHERE id = ?", (habit_id,))
    if start:
        return max(0, (today - date.fromisoformat(start[0]["start_date"])).days), None

    return 0, None
