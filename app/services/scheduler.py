import asyncio
import logging
from datetime import UTC, datetime

from app.db import get_db, get_setting, set_setting

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


async def _run_export_task(db) -> None:
    from app.services.markdown_export import write_export

    await write_export(db, scope="weekly")
    logger.info("Scheduled markdown export complete")


async def _check_and_run(db) -> None:
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
                await _run_export_task(db)
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


async def scheduler_loop() -> None:
    """Main scheduler loop. Wakes every TICK_SECONDS, checks for due tasks."""
    logger.info("Scheduler started (tick=%ds)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            db = await get_db()
            await _check_and_run(db)
        except Exception:
            logger.exception("Scheduler tick failed")
