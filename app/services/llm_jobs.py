"""Shared durability primitives for jobs that can incur an LLM charge."""

import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

import aiosqlite

from app.services.job_producers import ActiveWorkloadConflictError
from app.services.jobs import JOB_TERMINAL_HISTORY_MAX, EnqueueResult, enqueue_job, prune_terminal_jobs

if TYPE_CHECKING:
    from app.services.job_worker import JobContext

# Migration 029 repeats these kinds as a frozen SQL literal, because a migration
# must keep meaning when this tuple later grows. test_paid_llm_jobs asserts the
# two stay in step.
PAID_LLM_JOB_KINDS = (
    "andy_generation",
    "experiment_summary",
    "medical_import",
    "morning_briefing",
    "onboarding_enrichment",
    "wod_parse",
)

# One attempt, never automatic. A provider timeout does not prove the request was
# refused, so a second attempt could buy the same answer twice.
PAID_LLM_JOB_MAX_ATTEMPTS = 1

# Keys are built from identifiers and dates only. Nothing a user typed belongs
# in one: it is stored, logged and compared, and it must stay bounded.
_KEY_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def paid_llm_job_key(kind: str, *parts: str) -> str:
    """Build the identity of one unit of paid work: kind plus bounded parts."""
    if kind not in PAID_LLM_JOB_KINDS:
        raise ValueError("Unsupported paid LLM job kind")
    if not parts:
        raise ValueError("A paid LLM job key needs at least one identifying part")
    for part in parts:
        if not isinstance(part, str) or not _KEY_PART_RE.fullmatch(part):
            raise ValueError("Paid LLM job key parts must be short identifiers, dates or nonces")
    return ":".join((kind, *parts))


def _work_identity(kind: str, idempotency_key: str) -> tuple[str, str]:
    if kind not in PAID_LLM_JOB_KINDS:
        raise ValueError("Unsupported paid LLM job kind")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
        raise ValueError("LLM idempotency key must contain between 1 and 200 characters")
    if idempotency_key != idempotency_key.strip():
        raise ValueError("LLM idempotency key cannot have surrounding whitespace")
    return kind, idempotency_key


PAID_LLM_TRIGGERS = frozenset({"manual", "scheduled"})


def paid_llm_trigger(value: str) -> str:
    if value not in PAID_LLM_TRIGGERS:
        raise ValueError("Paid LLM trigger must be manual or scheduled")
    return value


async def llm_result_published(db: aiosqlite.Connection, kind: str, idempotency_key: str) -> bool:
    """True when this exact work already reached the user, job row or not.

    Terminal jobs are pruned; the ledger is not. A handler that crashed between
    its domain commit and completion must not pay the provider twice on retry.
    """
    normalized_kind, normalized_key = _work_identity(kind, idempotency_key)
    rows = await db.execute_fetchall("SELECT kind FROM llm_publications WHERE idempotency_key = ?", (normalized_key,))
    if not rows:
        return False
    if rows[0]["kind"] != normalized_kind:
        raise RuntimeError("LLM publication key belongs to another job kind")
    return True


async def record_llm_publication(
    db: aiosqlite.Connection,
    kind: str,
    idempotency_key: str,
    job_id: int,
) -> None:
    """Mark the work published. Callers must run this inside the domain write's
    own transaction, so the marker and the result commit or roll back together."""
    normalized_kind, normalized_key = _work_identity(kind, idempotency_key)
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
        raise ValueError("LLM publication job id must be a positive integer")
    await db.execute(
        "INSERT INTO llm_publications(idempotency_key, kind, job_id) VALUES (?, ?, ?)",
        (normalized_key, normalized_kind, job_id),
    )


async def enqueue_paid_llm_job(
    db: aiosqlite.Connection,
    kind: str,
    payload: Mapping[str, Any],
    *,
    idempotency_key: str,
) -> EnqueueResult:
    normalized_kind, normalized_key = _work_identity(kind, idempotency_key)
    existing = await db.execute_fetchall("SELECT id, kind FROM jobs WHERE idempotency_key = ?", (normalized_key,))
    if existing:
        if existing[0]["kind"] != normalized_kind:
            raise ActiveWorkloadConflictError("LLM idempotency key belongs to another job kind")
        return EnqueueResult(job_id=existing[0]["id"], created=False)

    queued = await db.execute_fetchall(
        "SELECT id FROM jobs WHERE kind = ? AND status = 'queued' ORDER BY id LIMIT 1",
        (normalized_kind,),
    )
    if queued:
        raise ActiveWorkloadConflictError(f"Another {normalized_kind} job is already queued")

    try:
        result = await enqueue_job(
            db,
            normalized_kind,
            payload,
            idempotency_key=normalized_key,
            max_attempts=PAID_LLM_JOB_MAX_ATTEMPTS,
            retry_policy="manual",
        )
    except sqlite3.IntegrityError as exc:
        # The partial unique index from migration 029 lost a race with a
        # concurrent enqueue of the same kind.
        raise ActiveWorkloadConflictError(f"Another {normalized_kind} job is already queued") from exc
    if result.created:
        await prune_terminal_jobs(db, normalized_kind, keep=JOB_TERMINAL_HISTORY_MAX - 1)
    return result


async def run_paid_llm_job(
    context: "JobContext",
    kind: str,
    idempotency_key: str,
    produce: Callable[[], Awaitable[Any]],
    publish: Callable[[Any], Awaitable[Mapping[str, Any]]],
) -> Mapping[str, Any]:
    """Buy one LLM answer at most once, then publish it atomically.

    produce() performs the provider call and must not hold a transaction open,
    because SQLite has a single writer and the call takes seconds. publish()
    runs inside one immediate transaction together with the ledger insert, so a
    crash can leave the answer either fully visible or fully absent, never a
    charge with nothing to show for it.
    """
    from app.services.job_worker import AmbiguousJobError
    from app.services.llm import LLMCallAmbiguousError

    if await llm_result_published(context.db, kind, idempotency_key):
        # A previous attempt already committed this exact work. Paying again
        # would buy a second copy of an answer the user can already see.
        return {"published": False, "reason": "already_published"}

    if context.db.in_transaction:
        raise RuntimeError("A paid LLM call must not run inside an open transaction")
    try:
        produced = await produce()
    except LLMCallAmbiguousError as exc:
        raise AmbiguousJobError(
            "The AI provider may already have charged for this request. Check it before retrying."
        ) from exc

    await context.db.execute("BEGIN IMMEDIATE")
    try:
        summary = await publish(produced)
        await record_llm_publication(context.db, kind, idempotency_key, context.job_id)
        await context.db.commit()
    except BaseException:
        await context.db.rollback()
        raise
    return {"published": True, **dict(summary)}
