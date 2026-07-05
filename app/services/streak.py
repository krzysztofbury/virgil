from datetime import date, timedelta


async def get_week_clean(db) -> tuple[int, int, int]:
    """Current-week clean rate (Gola method: 75%/week target), Monday..today.

    clean = days elapsed this week minus relapse days this week; pct over elapsed.
    A single slip drops the rate (e.g. 6/7 = 86%) but never hard-resets to 0.
    Returns (clean_days, days_elapsed, pct).
    """
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    elapsed = (today - monday).days + 1  # 1..7 inclusive
    rows = await db.execute_fetchall(
        "SELECT COUNT(DISTINCT date) AS c FROM pmo_events WHERE event_type = 'relapse' AND date BETWEEN ? AND ?",
        (monday.isoformat(), today.isoformat()),
    )
    relapses = rows[0]["c"] if rows else 0
    clean = max(0, elapsed - relapses)
    pct = round(clean / elapsed * 100) if elapsed else 0
    return clean, elapsed, pct


async def get_streak(db) -> tuple[int, date | None]:
    """Return (streak_days, last_relapse_date).
    Streak = days since last relapse. If no relapse, days since feniks start.
    """
    today = date.today()

    last_relapse = await db.execute_fetchall(
        "SELECT date FROM pmo_events WHERE event_type = 'relapse' ORDER BY date DESC LIMIT 1"
    )
    if last_relapse:
        relapse_date = date.fromisoformat(last_relapse[0]["date"])
        return (today - relapse_date).days, relapse_date

    conf = await db.execute_fetchall("SELECT start_date FROM feniks_config WHERE id = 1")
    if conf:
        start = date.fromisoformat(conf[0]["start_date"])
        return (today - start).days, None

    return 0, None
