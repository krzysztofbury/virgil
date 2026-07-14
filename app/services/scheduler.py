import asyncio
import logging
from datetime import UTC, datetime

from app.central_db import get_active_users
from app.db import get_setting, set_setting
from app.user_db import close_user_db, open_user_db

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


def _hours_since(iso_str: str) -> float:
    """Return hours elapsed since an ISO datetime string. Returns inf if empty."""
    if not iso_str:
        return float("inf")
    try:
        then = datetime.fromisoformat(iso_str)
        if then.tzinfo is None:
            then = then.replace(tzinfo=UTC)
        return (datetime.now(UTC) - then).total_seconds() / 3600
    except (ValueError, TypeError):
        return float("inf")


async def _run_backup_task(db) -> None:
    from app.services.backup import run_backup

    await run_backup(db)


async def _run_oura_sync_task(db) -> None:
    from app.services.oura_api import sync_oura_from_api

    count = await sync_oura_from_api(db)
    logger.info("Scheduled Oura sync: %d days", count)


async def _run_export_task(db, user_id: str) -> None:
    from app.services.markdown_export import export_filename_for, write_export

    # Per-user filename — all users share one SECOND_BRAIN_PATH.
    filename = await export_filename_for(db, user_id)
    await write_export(db, scope="weekly", filename=filename)
    logger.info("Scheduled markdown export complete: %s", filename)


async def _run_briefing_task(db) -> None:
    from app.services.briefing import generate_briefing

    await generate_briefing(db)
    logger.info("Scheduled morning briefing generated")


# Morning briefings generate once per local day, but not before people wake up —
# Oura sleep data usually lands after the night ends.
BRIEFING_EARLIEST_HOUR = 6
# On failure (LLM down, no provider), wait before retrying instead of hammering
# the LLM every 60-second tick.
BRIEFING_RETRY_HOURS = 1.0


def _briefing_due(now: datetime, last_day: str, last_attempt: str) -> bool:
    """Pure gating logic for the scheduled morning briefing."""
    if now.hour < BRIEFING_EARLIEST_HOUR:
        return False
    if last_day == now.date().isoformat():
        return False
    return _hours_since(last_attempt) >= BRIEFING_RETRY_HOURS


async def _check_and_run(db, user_id: str) -> None:
    """Check all scheduled tasks and run those that are due."""
    now_iso = datetime.now(UTC).isoformat()

    # Backup
    if await get_setting(db, "backup_enabled", "0") == "1":
        interval = float(await get_setting(db, "backup_interval_hours", "24"))
        last_run = await get_setting(db, "backup_last_run", "")
        if _hours_since(last_run) >= interval:
            try:
                await _run_backup_task(db)
                await set_setting(db, "backup_last_run", now_iso)
            except Exception:
                logger.exception("Scheduled backup failed")

    # Markdown export (virgil.md → second-brain for OpenClaw)
    if await get_setting(db, "export_enabled", "0") == "1":
        interval = float(await get_setting(db, "export_interval_hours", "6"))
        last_run = await get_setting(db, "export_last_run", "")
        if _hours_since(last_run) >= interval:
            try:
                await _run_export_task(db, user_id)
                await set_setting(db, "export_last_run", now_iso)
            except Exception:
                logger.exception("Scheduled markdown export failed")

    # Oura auto-sync
    if await get_setting(db, "oura_sync_enabled", "0") == "1":
        # Check that Oura is actually connected
        oura_row = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
        if oura_row and oura_row[0]["status"] == "connected":
            interval = float(await get_setting(db, "oura_sync_interval_hours", "6"))
            last_run = await get_setting(db, "oura_sync_last_run", "")
            if _hours_since(last_run) >= interval:
                try:
                    await _run_oura_sync_task(db)
                    await set_setting(db, "oura_sync_last_run", now_iso)
                except Exception:
                    logger.exception("Scheduled Oura sync failed")

    # Morning briefing — once per local day, after BRIEFING_EARLIEST_HOUR.
    if await get_setting(db, "briefing_enabled", "0") == "1":
        from app.services.llm import llm_available

        last_day = await get_setting(db, "briefing_last_day", "")
        last_attempt = await get_setting(db, "briefing_last_attempt", "")
        if _briefing_due(datetime.now(), last_day, last_attempt) and await llm_available(db):
            await set_setting(db, "briefing_last_attempt", now_iso)
            try:
                await _run_briefing_task(db)
                await set_setting(db, "briefing_last_day", datetime.now().date().isoformat())
            except Exception:
                logger.exception("Scheduled briefing failed")


async def scheduler_loop() -> None:
    """Main scheduler loop. Wakes every TICK_SECONDS, checks for due tasks."""
    logger.info("Scheduler started (tick=%ds)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            users = await get_active_users()
            for user in users:
                user_db = None
                try:
                    user_db = await open_user_db(user["db_filename"])
                    await _check_and_run(user_db, user["id"])
                except Exception:
                    logger.exception("Scheduler failed for user %s", user["email"])
                finally:
                    if user_db is not None:
                        await close_user_db(user_db)
        except Exception:
            logger.exception("Scheduler tick failed")
