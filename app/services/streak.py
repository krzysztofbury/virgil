from datetime import date


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
