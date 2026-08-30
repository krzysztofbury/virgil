import asyncio
import logging
import os
import socket
from datetime import UTC, datetime

from app.central_db import get_active_users
from app.db import get_setting, set_setting
from app.services.job_producers import (
    BACKUP_JOB_KIND,
    MARKDOWN_EXPORT_JOB_KIND,
    OURA_SYNC_JOB_KIND,
    ActiveWorkloadConflictError,
    enqueue_backup_job,
    enqueue_markdown_export_job,
    enqueue_oura_sync_job,
    scheduled_job_key,
)
from app.user_db import close_user_db, open_user_db

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
USERS_PER_TICK_MAX = 100
USERS_CONCURRENT_MAX = 4
WORKER_ID = f"{socket.gethostname()[:60]}:{os.getpid()}"
_user_offset = 0


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


async def _enqueue_backup_if_due(db, now: datetime) -> None:
    try:
        if await get_setting(db, "backup_enabled", "1") != "1":
            return
        interval = float(await get_setting(db, "backup_interval_hours", "24"))
        if _hours_since(await get_setting(db, "backup_last_run", "")) < interval:
            return
        result = await enqueue_backup_job(
            db,
            trigger="scheduled",
            idempotency_key=scheduled_job_key(BACKUP_JOB_KIND, interval, now=now),
        )
        if result.created:
            logger.info("Scheduled backup job queued: %d", result.job_id)
    except ActiveWorkloadConflictError:
        logger.info("Scheduled backup is waiting for queued backup work")
    except Exception:
        logger.exception("Failed to enqueue scheduled backup")


async def _enqueue_export_if_due(db, now: datetime) -> None:
    try:
        if await get_setting(db, "export_enabled", "0") != "1":
            return
        interval = float(await get_setting(db, "export_interval_hours", "6"))
        if _hours_since(await get_setting(db, "export_last_run", "")) < interval:
            return
        result = await enqueue_markdown_export_job(
            db,
            trigger="scheduled",
            scope="weekly",
            sections=None,
            idempotency_key=scheduled_job_key(MARKDOWN_EXPORT_JOB_KIND, interval, now=now),
        )
        if result.created:
            logger.info("Scheduled markdown export job queued: %d", result.job_id)
    except ActiveWorkloadConflictError:
        logger.info("Scheduled markdown export is waiting for queued export work")
    except Exception:
        logger.exception("Failed to enqueue scheduled markdown export")


async def _enqueue_oura_if_due(db, now: datetime) -> None:
    try:
        if await get_setting(db, "oura_sync_enabled", "0") != "1":
            return
        rows = await db.execute_fetchall("SELECT status FROM integrations WHERE provider = 'oura'")
        if not rows or rows[0]["status"] != "connected":
            return
        interval = float(await get_setting(db, "oura_sync_interval_hours", "6"))
        if _hours_since(await get_setting(db, "oura_sync_last_run", "")) < interval:
            return
        result = await enqueue_oura_sync_job(
            db,
            trigger="scheduled",
            days_back=30,
            idempotency_key=scheduled_job_key(OURA_SYNC_JOB_KIND, interval, now=now),
        )
        if result.created:
            logger.info("Scheduled Oura sync job queued: %d", result.job_id)
    except ActiveWorkloadConflictError:
        logger.info("Scheduled Oura sync is waiting for queued Oura work")
    except Exception:
        logger.exception("Failed to enqueue scheduled Oura sync")


async def _enqueue_due_jobs(db) -> None:
    now = datetime.now(UTC)
    await _enqueue_backup_if_due(db, now)
    await _enqueue_export_if_due(db, now)
    await _enqueue_oura_if_due(db, now)


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
    """Run scheduled work that has not migrated to durable jobs yet."""
    now_iso = datetime.now(UTC).isoformat()

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


def _select_users_for_tick(users: list[dict]) -> list[dict]:
    """Take one bounded rotating slice so no active user is permanently starved."""
    global _user_offset
    if not users:
        _user_offset = 0
        return []
    count = min(len(users), USERS_PER_TICK_MAX)
    start = _user_offset % len(users)
    selected = [users[(start + index) % len(users)] for index in range(count)]
    _user_offset = (start + count) % len(users)
    return selected


async def _run_scheduled_user(user: dict, semaphore: asyncio.Semaphore) -> None:
    from app.services.job_worker import run_jobs_for_user

    async with semaphore:
        user_db = None
        try:
            user_db = await open_user_db(user["db_filename"])
            await _enqueue_due_jobs(user_db)
            await run_jobs_for_user(
                user_db,
                user["id"],
                user["db_filename"],
                worker_id=WORKER_ID,
            )
            await _check_and_run(user_db, user["id"])
        except Exception:
            logger.exception("Scheduler failed for user %s", user["email"])
        finally:
            if user_db is not None:
                try:
                    await close_user_db(user_db)
                except Exception:
                    logger.exception("Scheduler failed to close database for user %s", user["email"])


async def scheduler_tick() -> None:
    """Run one bounded scheduler and durable-worker pass."""
    from app.services.backup import maybe_backup_central

    users = await get_active_users()
    selected_users = _select_users_for_tick(users)
    if len(users) > len(selected_users):
        logger.warning("Scheduler rotating batch: processing %d of %d users", len(selected_users), len(users))
    semaphore = asyncio.Semaphore(USERS_CONCURRENT_MAX)
    await asyncio.gather(*(_run_scheduled_user(user, semaphore) for user in selected_users))

    # Per-user backups never cover the registry. This function self-limits to
    # once per 24 hours.
    await maybe_backup_central()


async def scheduler_loop() -> None:
    """Main scheduler loop. Wakes every TICK_SECONDS and runs one finite tick."""
    logger.info("Scheduler started (tick=%ds)", TICK_SECONDS)
    while True:
        await asyncio.sleep(TICK_SECONDS)
        try:
            await scheduler_tick()
        except Exception:
            logger.exception("Scheduler tick failed")
