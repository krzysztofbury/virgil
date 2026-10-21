"""Oura values against their own recent past: the mean of the previous days.

A rule, not an interpretation: a page states the two numbers, their difference
and, for Oura's own scores, one word. Nothing here generates advice and nothing
calls an LLM - a page that states a verdict was explicitly not wanted.
"""

from datetime import date, timedelta

BASELINE_DAYS = 7
# Oura's own scores move a few points day to day without meaning anything. Below
# this the honest word is "steady", which is why a threshold exists rather than a
# strict comparison.
BASELINE_TOLERANCE = 3
# How far back the previous scored days may be looked for.
BASELINE_LOOKBACK_DAYS = 30


def baseline(values: list, today_value) -> dict:
    """today_value against the mean of the first BASELINE_DAYS of `values`.

    `values` must exclude today and be newest first. A missing value is skipped,
    so a gap shortens the window instead of padding it with zero.
    """
    known = [value for value in values if value is not None][:BASELINE_DAYS]
    if not known or today_value is None:
        return {"today": today_value, "baseline": None, "delta": None, "status": "", "days": len(known)}
    mean = round(sum(known) / len(known))
    delta = round(today_value - mean)
    if delta >= BASELINE_TOLERANCE:
        status = "above"
    elif delta <= -BASELINE_TOLERANCE:
        status = "below"
    else:
        status = "steady"
    return {"today": today_value, "baseline": mean, "delta": delta, "status": status, "days": len(known)}


def readiness_baseline(rows: list[dict], today_score: int | None) -> dict:
    """Today's readiness against the mean of the previous BASELINE_DAYS days."""
    return baseline([row.get("readiness_score") for row in rows], today_score)


async def baselines_for(db, day: date, today_row: dict | None, columns: tuple[str, ...]) -> dict[str, dict]:
    """Each column of today's oura_daily row against its previous scored days."""
    if not today_row:
        return {}
    rows = await db.execute_fetchall(
        f"SELECT {', '.join(columns)} FROM oura_daily WHERE date < ? AND date >= ? ORDER BY date DESC",
        (day.isoformat(), (day - timedelta(days=BASELINE_LOOKBACK_DAYS)).isoformat()),
    )
    return {column: baseline([row[column] for row in rows], today_row.get(column)) for column in columns}
