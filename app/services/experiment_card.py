"""One active experiment as the dashboard shows it: progress plus today's quick log.

Used by the dashboard and by the entry route, which redraws a card in place after
a tap on it.
"""

from datetime import date

from app.services.experiment_weeks import experiment_calendar, experiment_week_number


async def experiment_card(db, experiment: dict, today: date) -> dict:
    card = dict(experiment)
    start = date.fromisoformat(card["start_date"])
    _, end = experiment_calendar(start, card["num_weeks"])
    card["not_started"] = today < start
    card["current_week"] = 0 if card["not_started"] else experiment_week_number(start, card["num_weeks"], today)
    card["end_date"] = end.isoformat()
    # Logging is open on the experiment's own days only, as the entry route enforces.
    card["loggable"] = card["status"] == "active" and start <= today <= end

    totals = await db.execute_fetchall(
        "SELECT COALESCE(SUM(CASE WHEN eat.kind = 'duration' THEN ee.value ELSE 0 END), 0) AS minutes, "
        "COUNT(*) AS entries "
        "FROM experiment_entries ee JOIN experiment_activity_types eat ON ee.activity_type_id = eat.id "
        "WHERE ee.experiment_id = ?",
        (card["id"],),
    )
    minutes = totals[0]["minutes"] if totals else 0
    entries = totals[0]["entries"] if totals else 0
    # Kind-aware label: minutes for training-style experiments, entry count otherwise.
    card["total_minutes"] = minutes
    card["logged_label"] = f"{minutes}m" if minutes else f"{entries} logged"

    metric_rows = await db.execute_fetchall(
        "SELECT id, name, kind, color FROM experiment_activity_types WHERE experiment_id = ? ORDER BY id",
        (card["id"],),
    )
    today_rows = await db.execute_fetchall(
        "SELECT activity_type_id, value FROM experiment_entries WHERE experiment_id = ? AND date = ? ORDER BY id",
        (card["id"], today.isoformat()),
    )
    by_metric: dict[int, list[int]] = {}
    for row in today_rows:
        by_metric.setdefault(row["activity_type_id"], []).append(row["value"])
    metrics = []
    for row in metric_rows:
        values = by_metric.get(row["id"], [])
        if row["kind"] in ("duration", "count"):
            logged = sum(values) if values else None
        else:  # boolean and scale: the latest answer is today's answer
            logged = values[-1] if values else None
        metrics.append({**dict(row), "today": logged})
    card["metrics"] = metrics
    return card
