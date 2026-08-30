"""Bounded execution for durable per-user jobs."""

import asyncio
import contextlib
import logging
import os
import secrets
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any

import aiosqlite

from app.services.jobs import (
    JOB_RETRY_DELAY_SECONDS_MAX,
    RecoveryResult,
    claim_next_job,
    complete_job,
    decode_job_payload,
    fail_job,
    heartbeat_job,
    recover_stale_jobs,
)
from app.user_db import close_user_db, open_user_db

logger = logging.getLogger(__name__)

JOBS_PER_USER_MAX = 10
DEFAULT_JOBS_PER_USER = 1
DEFAULT_HEARTBEAT_SECONDS = 30.0
DEFAULT_STALE_SECONDS = 120
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 900.0
EXECUTION_TIMEOUT_SECONDS_MAX = 3600.0
RETRY_BASE_SECONDS = 60
SQLITE_BUSY_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class JobContext:
    db: aiosqlite.Connection
    user_id: str
    job_id: int
    attempt: int


# Handlers are trusted application code. They must propagate cancellation and
# must not await external I/O while their database connection is in a transaction.
JobHandler = Callable[[JobContext, Mapping[str, Any]], Awaitable[Mapping[str, Any] | None]]
DbOpener = Callable[[str], Awaitable[aiosqlite.Connection]]
DbCloser = Callable[[aiosqlite.Connection], Awaitable[None]]

# Payload data can never select an import path or arbitrary callable.
from app.services.job_handlers import (  # noqa: E402
    handle_andy_generation,
    handle_backup,
    handle_experiment_summary,
    handle_markdown_export,
    handle_medical_import,
    handle_morning_briefing,
    handle_onboarding_enrichment,
    handle_oura_sync,
    handle_wod_parse,
)

JOB_HANDLERS: Mapping[str, JobHandler] = MappingProxyType(
    {
        "andy_generation": handle_andy_generation,
        "backup": handle_backup,
        "experiment_summary": handle_experiment_summary,
        "markdown_export": handle_markdown_export,
        "medical_import": handle_medical_import,
        "morning_briefing": handle_morning_briefing,
        "onboarding_enrichment": handle_onboarding_enrichment,
        "oura_sync": handle_oura_sync,
        "wod_parse": handle_wod_parse,
    }
)


class AmbiguousJobError(RuntimeError):
    """The handler may have completed an external side effect."""

    def __init__(self, public_error: str = "Job outcome needs review.") -> None:
        super().__init__(public_error)
        self.public_error = public_error


# A woken pass may run a short burst, so two quick clicks do not leave the
# second job for the tick. It stays small: each job can hold the worker for
# DEFAULT_EXECUTION_TIMEOUT_SECONDS.
WAKE_JOBS_MAX = 3
# Concurrent woken passes are pointless past a handful - the claim is
# single-runner per database, so the rest find nothing and exit - but the bound
# is what stops a click-happy page spawning tasks without limit.
WAKE_TASKS_MAX = 8
_wake_tasks: set[asyncio.Task] = set()


async def _wake_once(user_id: str, db_filename: str, worker_id: str) -> None:
    control_db = None
    try:
        control_db = await open_user_db(db_filename)
        await run_jobs_for_user(control_db, user_id, db_filename, worker_id=worker_id, max_jobs=WAKE_JOBS_MAX)
    except Exception:
        logger.exception("Woken worker pass failed for user %s", user_id)
    finally:
        if control_db is not None:
            with contextlib.suppress(Exception):
                await close_user_db(control_db)


def wake_worker(user_id: str, db_filename: str) -> bool:
    """Start a worker pass now instead of waiting for the next scheduler tick.

    Safe to race the tick: claims are atomic and one runner per database is a
    database-level invariant, so a woken pass either claims the job first or
    finds nothing and exits. Returns whether a pass was started, which is
    advisory - the tick still picks the job up if it was not.
    """
    from app.config import WORKER_WAKE

    if not WORKER_WAKE:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if len(_wake_tasks) >= WAKE_TASKS_MAX:
        logger.warning("Skipping a worker wake: %d passes already in flight", len(_wake_tasks))
        return False
    worker_id = f"wake:{socket.gethostname()[:52]}:{os.getpid()}"
    # Held in a set: a task with no strong reference can be garbage collected
    # mid-run, which would look exactly like the job silently never starting.
    task = loop.create_task(_wake_once(user_id, db_filename, worker_id))
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)
    return True


class VisibleJobError(RuntimeError):
    """A handler failure whose message the handler certifies as safe to show.

    Everything else fails with a generic message, because an arbitrary
    exception string can carry payload or credential fragments. A paid LLM call
    is the case worth the exception: "check your API key" is the whole answer,
    and hiding it in a container log is what made a failed generation
    indistinguishable from one that silently did nothing.
    """

    def __init__(self, public_error: str) -> None:
        super().__init__(public_error)
        self.public_error = public_error


class LeaseLostError(RuntimeError):
    """The running attempt no longer owns its durable lease."""


@dataclass(frozen=True)
class JobRunResult:
    job_id: int
    status: str


@dataclass(frozen=True)
class WorkerBatchResult:
    recovered: tuple[RecoveryResult, ...]
    jobs: tuple[JobRunResult, ...]


def _validate_limits(
    max_jobs: int,
    heartbeat_seconds: float,
    stale_seconds: int,
    execution_timeout_seconds: float,
) -> None:
    if not isinstance(max_jobs, int) or isinstance(max_jobs, bool) or not 1 <= max_jobs <= JOBS_PER_USER_MAX:
        raise ValueError(f"max_jobs must be between 1 and {JOBS_PER_USER_MAX}")
    if not isinstance(stale_seconds, int) or isinstance(stale_seconds, bool) or not 2 <= stale_seconds <= 86400:
        raise ValueError("stale_seconds must be between 2 and 86400")
    if not isinstance(heartbeat_seconds, (int, float)) or isinstance(heartbeat_seconds, bool):
        raise ValueError("heartbeat_seconds must be numeric")
    minimum_stale_seconds = heartbeat_seconds * 2 + SQLITE_BUSY_TIMEOUT_SECONDS
    if heartbeat_seconds < 0.01 or stale_seconds < minimum_stale_seconds:
        raise ValueError("stale_seconds must cover two heartbeats plus the SQLite busy timeout")
    if not isinstance(execution_timeout_seconds, (int, float)) or isinstance(execution_timeout_seconds, bool):
        raise ValueError("execution_timeout_seconds must be numeric")
    if not heartbeat_seconds <= execution_timeout_seconds <= EXECUTION_TIMEOUT_SECONDS_MAX:
        raise ValueError(
            f"execution_timeout_seconds must be between heartbeat_seconds and {EXECUTION_TIMEOUT_SECONDS_MAX}"
        )


def _retry_delay_seconds(job: Mapping[str, Any]) -> int:
    attempt = max(1, int(job["attempts"]))
    base = min(JOB_RETRY_DELAY_SECONDS_MAX, RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 10)))
    jitter = secrets.randbelow(max(1, base // 4 + 1))
    return min(JOB_RETRY_DELAY_SECONDS_MAX, base + jitter)


async def _await_handler(
    handler: JobHandler,
    context: JobContext,
    payload: Mapping[str, Any],
    control_db: aiosqlite.Connection,
    claim_token: str,
    heartbeat_seconds: float,
    execution_timeout_seconds: float,
) -> Mapping[str, Any] | None:
    task = asyncio.create_task(handler(context, payload))
    try:
        async with asyncio.timeout(execution_timeout_seconds):
            while True:
                done, _ = await asyncio.wait({task}, timeout=heartbeat_seconds)
                if done:
                    return await task
                if context.db.in_transaction:
                    raise AmbiguousJobError("Job handler held an open database transaction across an await.")
                if not await heartbeat_job(control_db, context.job_id, claim_token):
                    raise LeaseLostError("Job lease was lost while the handler was running")
    except TimeoutError as exc:
        raise AmbiguousJobError("Job execution timed out; outcome needs review.") from exc
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _run_registered_handler(
    handler: JobHandler,
    control_db: aiosqlite.Connection,
    job: Mapping[str, Any],
    payload: Mapping[str, Any],
    user_id: str,
    db_filename: str,
    opener: DbOpener,
    closer: DbCloser,
    heartbeat_seconds: float,
    execution_timeout_seconds: float,
) -> Mapping[str, Any] | None:
    handler_db = await opener(db_filename)
    try:
        if handler_db.in_transaction:
            raise RuntimeError("Handler database connection must not have an active transaction")
        context = JobContext(
            db=handler_db,
            user_id=user_id,
            job_id=job["id"],
            attempt=job["attempts"],
        )
        result = await _await_handler(
            handler,
            context,
            payload,
            control_db,
            job["claim_token"],
            heartbeat_seconds,
            execution_timeout_seconds,
        )
        if handler_db.in_transaction:
            await handler_db.rollback()
            raise AmbiguousJobError("Job handler returned with an uncommitted database transaction.")
    except BaseException:
        try:
            await closer(handler_db)
        except Exception:
            logger.exception("Handler database cleanup failed while propagating an execution error")
        raise
    try:
        await closer(handler_db)
    except Exception as exc:
        raise AmbiguousJobError("Job cleanup failed after execution; outcome needs review.") from exc
    return result


async def _execute_claimed_job(
    control_db: aiosqlite.Connection,
    job: Mapping[str, Any],
    user_id: str,
    db_filename: str,
    handlers: Mapping[str, JobHandler],
    opener: DbOpener,
    closer: DbCloser,
    heartbeat_seconds: float,
    execution_timeout_seconds: float,
) -> JobRunResult:
    handler = handlers.get(job["kind"])
    if handler is None:
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            "Unsupported job kind.",
            ambiguous=True,
        )
        return JobRunResult(job["id"], status or "lease_lost")

    try:
        payload = decode_job_payload(job)
    except (TypeError, ValueError):
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            "Stored job payload is invalid.",
            ambiguous=True,
        )
        return JobRunResult(job["id"], status or "lease_lost")

    try:
        result = await _run_registered_handler(
            handler,
            control_db,
            job,
            payload,
            user_id,
            db_filename,
            opener,
            closer,
            heartbeat_seconds,
            execution_timeout_seconds,
        )
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            raise
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            "Job handler was cancelled; outcome needs review.",
            ambiguous=True,
        )
        return JobRunResult(job["id"], status or "lease_lost")
    except LeaseLostError:
        return JobRunResult(job["id"], "lease_lost")
    except AmbiguousJobError as exc:
        logger.warning("Job %d (%s) needs attention: %s", job["id"], job["kind"], exc)
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            exc.public_error,
            ambiguous=True,
        )
        return JobRunResult(job["id"], status or "lease_lost")
    except VisibleJobError as exc:
        logger.warning("Job %d (%s) failed: %s", job["id"], job["kind"], exc)
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            exc.public_error,
            retry_delay_seconds=_retry_delay_seconds(job),
        )
        return JobRunResult(job["id"], status or "lease_lost")
    except Exception:
        logger.exception("Job %d (%s) failed", job["id"], job["kind"])
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            "Job execution failed.",
            retry_delay_seconds=_retry_delay_seconds(job),
        )
        return JobRunResult(job["id"], status or "lease_lost")

    try:
        completed = await complete_job(control_db, job["id"], job["claim_token"], result)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Job %d (%s) ran but its result could not be persisted", job["id"], job["kind"])
        status = await fail_job(
            control_db,
            job["id"],
            job["claim_token"],
            "Job ran but its result could not be persisted; outcome needs review.",
            ambiguous=True,
        )
        return JobRunResult(job["id"], status or "lease_lost")
    return JobRunResult(job["id"], "succeeded" if completed else "lease_lost")


async def run_jobs_for_user(
    control_db: aiosqlite.Connection,
    user_id: str,
    db_filename: str,
    *,
    worker_id: str,
    handlers: Mapping[str, JobHandler] | None = None,
    max_jobs: int = DEFAULT_JOBS_PER_USER,
    recovery_limit: int = 20,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    execution_timeout_seconds: float = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    handler_db_opener: DbOpener | None = None,
    handler_db_closer: DbCloser | None = None,
) -> WorkerBatchResult:
    """Recover stale leases, then execute a bounded number of jobs serially."""
    _validate_limits(max_jobs, heartbeat_seconds, stale_seconds, execution_timeout_seconds)
    selected_handlers = JOB_HANDLERS if handlers is None else handlers
    opener = handler_db_opener or open_user_db
    closer = handler_db_closer or close_user_db
    now = datetime.now(UTC)
    recovered = await recover_stale_jobs(
        control_db,
        now - timedelta(seconds=stale_seconds),
        now=now,
        limit=recovery_limit,
    )
    results: list[JobRunResult] = []
    for _ in range(max_jobs):
        job = await claim_next_job(control_db, worker_id)
        if job is None:
            break
        result = await _execute_claimed_job(
            control_db,
            job,
            user_id,
            db_filename,
            selected_handlers,
            opener,
            closer,
            heartbeat_seconds,
            execution_timeout_seconds,
        )
        results.append(result)
    return WorkerBatchResult(tuple(recovered), tuple(results))
