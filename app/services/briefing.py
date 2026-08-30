import logging
from datetime import date, timedelta

from app.db import get_setting
from app.services.llm import call_llm
from app.services.streak import get_streak

logger = logging.getLogger(__name__)


async def _gather_context(db) -> str:
    """Gather today's data for the briefing prompt."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_iso = today.isoformat()
    parts = []

    # Streak (only if Feniks enabled)
    feniks_enabled = await get_setting(db, "feature_no_porn", "0") == "1"
    if feniks_enabled:
        streak_days, _ = await get_streak(db)
        parts.append(f"No Porn streak: {streak_days} days clean")

    # Yesterday's log
    row = await db.execute_fetchall("SELECT * FROM daily_logs WHERE date = ?", (yesterday,))
    if row:
        log = dict(row[0])
        energy = log.get("energy", "?")
        parts.append(f"Yesterday ({yesterday}): energy {energy}/10")
        for field in ["andy_body_desc", "andy_spirit_desc", "andy_account_desc", "andy_relations_desc"]:
            desc = log.get(field, "")
            if desc:
                label = field.replace("andy_", "").replace("_desc", "").title()
                parts.append(f"  {label}: {desc}")
        if log.get("notes"):
            parts.append(f"  Notes: {log['notes']}")

    # Today's Oura
    oura = await db.execute_fetchall("SELECT * FROM oura_daily WHERE date = ?", (today_iso,))
    if oura:
        o = dict(oura[0])
        metrics = []
        if o.get("sleep_score"):
            metrics.append(f"sleep={o['sleep_score']}")
        if o.get("readiness_score"):
            metrics.append(f"readiness={o['readiness_score']}")
        if o.get("avg_hrv"):
            metrics.append(f"HRV={o['avg_hrv']:.0f}")
        if o.get("resting_hr"):
            metrics.append(f"RHR={o['resting_hr']:.0f}")
        if metrics:
            parts.append(f"Today's Oura: {', '.join(metrics)}")

    # Active experiments
    exps = await db.execute_fetchall(
        "SELECT title, num_weeks, start_date FROM experiments WHERE status = 'active' LIMIT 3"
    )
    if exps:
        exp_strs = []
        for row in exps:
            exp = dict(row)
            start = date.fromisoformat(exp["start_date"])
            week = max(1, min(exp["num_weeks"], (today - start).days // 7 + 1))
            exp_strs.append(f"{exp['title']} (week {week}/{exp['num_weeks']})")
        parts.append(f"Active experiments: {'; '.join(exp_strs)}")

    return "\n".join(parts)


BRIEFING_MAX_CHARS = 8000


async def generate_briefing_text(db, day_iso: str) -> str:
    """Buy one briefing from the provider. No writes, so nothing to roll back."""
    context = await _gather_context(db)

    system_prompt = (
        "You are Virgil, a personal development assistant. "
        "Generate a concise morning briefing (3-5 short paragraphs) in English. "
        "Include: a motivational opener based on the streak, key observations from yesterday's data, "
        "today's body metrics if available, and one actionable suggestion for the day. "
        "Be warm but direct. Use markdown formatting (bold for emphasis)."
    )
    user_prompt = f"Today is {day_iso}. Here is my current data:\n\n{context}"

    return await call_llm(db, system_prompt, user_prompt)


async def save_briefing(db, day_iso: str, content: str) -> int:
    """Store one day's briefing. The caller owns the transaction, because the
    briefing and its publication marker have to commit together."""
    text = content.strip()[:BRIEFING_MAX_CHARS]
    if not text:
        raise ValueError("The provider returned an empty briefing")
    await db.execute(
        "INSERT INTO daily_briefings (date, content) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET content = excluded.content, created_at = datetime('now')",
        (day_iso, text),
    )
    return len(text)


async def get_cached_briefing(db) -> str | None:
    """Get today's cached briefing, if any."""
    today_iso = date.today().isoformat()
    row = await db.execute_fetchall("SELECT content FROM daily_briefings WHERE date = ?", (today_iso,))
    return row[0]["content"] if row else None
