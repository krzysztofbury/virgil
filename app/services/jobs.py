"""Durable per-user job state transitions.

All write operations own a short BEGIN IMMEDIATE transaction. No handler or
external I/O belongs in this module, so a claimed job never keeps SQLite's
single-writer lock while work is running.
"""

import json
import math
import re
import secrets
import sqlite3
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

JOB_PAYLOAD_MAX = 16384
JOB_ERROR_MAX = 500
JOB_RESULT_MAX = 16384
JOB_RECOVERY_LIMIT_MAX = 100
JOB_RETRY_DELAY_SECONDS_MAX = 86400
JOB_JSON_DEPTH_MAX = 20
JOB_JSON_NODES_MAX = 1024
JOB_CLOCK_SKEW_SECONDS_MAX = 300
JOB_STATUS_LIST_LIMIT_MAX = 20
JOB_TERMINAL_HISTORY_MAX = 100

_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RETRY_POLICIES = {"automatic", "manual"}
_EXPLICIT_RETRY_STATUSES = {"failed", "cancelled", "needs_attention"}
_SINGLE_QUEUED_SUCCESSOR_KINDS = {"backup", "markdown_export", "oura_sync"}
_SINGLE_QUEUED_SUCCESSOR_INDEX = "idx_jobs_queued_workload_kind"
_WORKLOAD_TRIGGERS = {"manual", "scheduled", "webhook"}
_EXPORT_SCOPES = {"weekly", "monthly", "yearly", "all"}
_EXPORT_SECTIONS = {
    "daily_logs",
    "training",
    "body_measurements",
    "feniks",
    "oura",
    "life_scores",
    "experiments",
    "bloodwork",
    "goals",
}


class IdempotencyConflictError(ValueError):
    """The same idempotency key was reused for different work."""


@dataclass(frozen=True)
class EnqueueResult:
    job_id: int
    created: bool


@dataclass(frozen=True)
class RecoveryResult:
    job_id: int
    status: str


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Job timestamps must be timezone-aware")
    return current.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def _transition_time(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Job timestamps must be timezone-aware")
    current = current.astimezone(UTC)
    if current > datetime.now(UTC) + timedelta(seconds=JOB_CLOCK_SKEW_SECONDS_MAX):
        raise ValueError(f"Transition time cannot exceed clock time by more than {JOB_CLOCK_SKEW_SECONDS_MAX} seconds")
    return current


def _validate_json_shape(value: Any, *, field: str, limit: int) -> None:
    pending = [(value, 0)]
    scheduled_nodes = 1
    text_characters = 0
    while pending:
        item, depth = pending.pop()
        if depth > JOB_JSON_DEPTH_MAX:
            raise ValueError(f"{field} is too complex")
        if item is None or isinstance(item, (bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{field} contains a non-finite number")
            continue
        if isinstance(item, str):
            text_characters += len(item)
            if text_characters > limit:
                raise ValueError(f"{field} exceeds {limit} characters")
            continue
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field} keys must be strings")
                text_characters += len(key)
                if text_characters > limit:
                    raise ValueError(f"{field} exceeds {limit} characters")
                scheduled_nodes += 1
                if scheduled_nodes > JOB_JSON_NODES_MAX:
                    raise ValueError(f"{field} is too complex")
                pending.append((nested, depth + 1))
            continue
        if isinstance(item, (list, tuple)):
            for nested in item:
                scheduled_nodes += 1
                if scheduled_nodes > JOB_JSON_NODES_MAX:
                    raise ValueError(f"{field} is too complex")
                pending.append((nested, depth + 1))
            continue
        raise ValueError(f"{field} must contain only JSON values")


def _json_object(value: Mapping[str, Any] | None, *, field: str, limit: int) -> str:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    source = value if value is not None else {}
    _validate_json_shape(source, field=field, limit=limit)
    try:
        encoded = json.dumps(dict(source), allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return encoded


def _kind(value: str) -> str:
    if not _KIND_PATTERN.fullmatch(value or ""):
        raise ValueError("Job kind must be lowercase ASCII snake_case")
    return value


def _idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    if value != value.strip() or not 1 <= len(value) <= 200:
        raise ValueError("Idempotency key must contain 1 to 200 non-padding characters")
    return value


def _worker_id(value: str) -> str:
    if value != value.strip() or not 1 <= len(value) <= 100:
        raise ValueError("Worker id must contain 1 to 100 non-padding characters")
    return value


def _claim_token(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", value or ""):
        raise ValueError("Claim token must be 32 lowercase hexadecimal characters")
    return value


def _retry_delay(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= JOB_RETRY_DELAY_SECONDS_MAX:
        raise ValueError(f"Retry delay must be between 1 and {JOB_RETRY_DELAY_SECONDS_MAX} seconds")
    return value


def _public_error(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip() or "Job failed."
    return text[:JOB_ERROR_MAX]


@asynccontextmanager
async def _immediate_transaction(db: aiosqlite.Connection) -> AsyncIterator[None]:
    if db.in_transaction:
        raise RuntimeError("Job transition requires a connection with no active transaction")
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def enqueue_job(
    db: aiosqlite.Connection,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    *,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
    retry_policy: str = "manual",
    run_after: datetime | None = None,
) -> EnqueueResult:
    """Insert one job, or return identical work; scheduling is not part of job identity."""
    normalized_kind = _kind(kind)
    payload_json = _json_object(payload, field="payload", limit=JOB_PAYLOAD_MAX)
    normalized_key = _idempotency_key(idempotency_key)
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 100:
        raise ValueError("max_attempts must be between 1 and 100")
    if retry_policy not in _RETRY_POLICIES:
        raise ValueError("retry_policy must be automatic or manual")
    run_after_text = _timestamp(run_after)

    async with _immediate_transaction(db):
        inserted = await db.execute_fetchall(
            """INSERT INTO jobs
               (kind, payload_json, idempotency_key, max_attempts, retry_policy, run_after)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
               RETURNING id""",
            (normalized_kind, payload_json, normalized_key, max_attempts, retry_policy, run_after_text),
        )
        if inserted:
            return EnqueueResult(job_id=inserted[0]["id"], created=True)

        existing = await db.execute_fetchall(
            """SELECT id, kind, payload_json, max_attempts, retry_policy
               FROM jobs WHERE idempotency_key = ?""",
            (normalized_key,),
        )
        assert existing, "idempotency conflict returned no existing job"
        row = existing[0]
        semantics = (row["kind"], row["payload_json"], row["max_attempts"], row["retry_policy"])
        expected = (normalized_kind, payload_json, max_attempts, retry_policy)
        if semantics != expected:
            raise IdempotencyConflictError("Idempotency key already belongs to different work")
        return EnqueueResult(job_id=row["id"], created=False)


async def claim_next_job(
    db: aiosqlite.Connection, worker_id: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    """Atomically claim at most one due job and enforce one runner per user DB."""
    owner = _worker_id(worker_id)
    token = secrets.token_hex(16)
    now_text = _timestamp(_transition_time(now))
    async with _immediate_transaction(db):
        rows = await db.execute_fetchall(
            """UPDATE jobs
               SET status = 'running', attempts = attempts + 1,
                    locked_at = ?, locked_by = ?, claim_token = ?,
                   started_at = COALESCE(started_at, ?),
                   finished_at = NULL, last_error = '', updated_at = ?
               WHERE id = (
                   SELECT id FROM jobs
                   WHERE status = 'queued' AND run_after <= ? AND attempts < max_attempts
                     AND NOT EXISTS (SELECT 1 FROM jobs WHERE status = 'running')
                   ORDER BY run_after, id LIMIT 1
               )
               AND status = 'queued'
               RETURNING *""",
            (now_text, owner, token, now_text, now_text, now_text),
        )
        return dict(rows[0]) if rows else None


async def heartbeat_job(
    db: aiosqlite.Connection, job_id: int, claim_token: str, *, now: datetime | None = None
) -> bool:
    token = _claim_token(claim_token)
    now_text = _timestamp(_transition_time(now))
    async with _immediate_transaction(db):
        cursor = await db.execute(
            """UPDATE jobs SET locked_at = ?, updated_at = ?
               WHERE id = ? AND status = 'running' AND claim_token = ? AND locked_at <= ?""",
            (now_text, now_text, job_id, token, now_text),
        )
        return cursor.rowcount == 1


async def complete_job(
    db: aiosqlite.Connection,
    job_id: int,
    claim_token: str,
    result: Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    token = _claim_token(claim_token)
    result_json = _json_object(result, field="result", limit=JOB_RESULT_MAX)
    now_text = _timestamp(_transition_time(now))
    async with _immediate_transaction(db):
        cursor = await db.execute(
            """UPDATE jobs
               SET status = 'succeeded', result_json = ?, last_error = '',
                    locked_at = NULL, locked_by = NULL, claim_token = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ? AND locked_at <= ?""",
            (result_json, now_text, now_text, job_id, token, now_text),
        )
        return cursor.rowcount == 1


def _successor_covers_running(kind: str, running_json: str, successor_json: str) -> bool:
    try:
        running = json.loads(running_json)
        successor = json.loads(successor_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(running, dict) or not isinstance(successor, dict):
        return False
    if kind == "backup":
        return (
            set(running) == set(successor) == {"trigger"}
            and running["trigger"]
            in {
                "manual",
                "scheduled",
            }
            and successor["trigger"] in {"manual", "scheduled"}
        )
    if kind == "oura_sync":
        running_days = running.get("days_back")
        successor_days = successor.get("days_back")
        return (
            set(running) == set(successor) == {"days_back", "trigger"}
            and running["trigger"] in _WORKLOAD_TRIGGERS
            and successor["trigger"] in _WORKLOAD_TRIGGERS
            and isinstance(running_days, int)
            and not isinstance(running_days, bool)
            and isinstance(successor_days, int)
            and not isinstance(successor_days, bool)
            and 1 <= running_days <= successor_days <= 30
        )
    if kind == "markdown_export":
        if set(running) != set(successor) or set(running) != {"scope", "sections", "trigger"}:
            return False
        if running["trigger"] not in {"manual", "scheduled"} or successor["trigger"] not in {"manual", "scheduled"}:
            return False
        if running["scope"] not in _EXPORT_SCOPES or successor["scope"] != running["scope"]:
            return False
        for sections in (running["sections"], successor["sections"]):
            if sections is not None and (
                not isinstance(sections, list)
                or sections != sorted(set(sections))
                or any(section not in _EXPORT_SECTIONS for section in sections)
            ):
                return False
        return running["sections"] == successor["sections"]
    return False


async def _has_covering_queued_successor(
    db: aiosqlite.Connection,
    kind: str,
    running_payload_json: str,
    now_text: str,
) -> bool:
    if kind not in _SINGLE_QUEUED_SUCCESSOR_KINDS:
        return False
    indexes = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (_SINGLE_QUEUED_SUCCESSOR_INDEX,),
    )
    if not indexes:
        return False
    rows = await db.execute_fetchall(
        "SELECT id, payload_json FROM jobs WHERE kind = ? AND status = 'queued' ORDER BY id LIMIT 1",
        (kind,),
    )
    if not rows:
        return False
    if _successor_covers_running(kind, running_payload_json, rows[0]["payload_json"]):
        return True
    cursor = await db.execute(
        """UPDATE jobs
           SET status = 'cancelled', last_error = 'Did not cover interrupted running work.',
               finished_at = ?, updated_at = ?
           WHERE id = ? AND status = 'queued'""",
        (now_text, now_text, rows[0]["id"]),
    )
    assert cursor.rowcount == 1, "queued successor changed during an immediate transaction"
    return False


async def fail_job(
    db: aiosqlite.Connection,
    job_id: int,
    claim_token: str,
    public_error: str,
    *,
    retry_delay_seconds: int = 60,
    ambiguous: bool = False,
    now: datetime | None = None,
) -> str | None:
    """Release an owned job to retry or a terminal state without leaking exception detail."""
    token = _claim_token(claim_token)
    delay = _retry_delay(retry_delay_seconds)
    now_value = _transition_time(now)
    now_text = _timestamp(now_value)
    retry_at = _timestamp(now_value + timedelta(seconds=delay))
    error = _public_error(public_error)

    async with _immediate_transaction(db):
        rows = await db.execute_fetchall(
            """SELECT kind, payload_json, attempts, max_attempts, retry_policy FROM jobs
               WHERE id = ? AND status = 'running' AND claim_token = ? AND locked_at <= ?""",
            (job_id, token, now_text),
        )
        if not rows:
            return None
        row = rows[0]
        if ambiguous:
            status = "needs_attention"
        elif row["retry_policy"] == "automatic" and row["attempts"] < row["max_attempts"]:
            has_successor = await _has_covering_queued_successor(db, row["kind"], row["payload_json"], now_text)
            status = "failed" if has_successor else "queued"
        else:
            status = "failed"
        finished_at = None if status == "queued" else now_text
        run_after = retry_at if status == "queued" else now_text
        cursor = await db.execute(
            """UPDATE jobs
                SET status = ?, run_after = ?, locked_at = NULL, locked_by = NULL, claim_token = NULL,
                   last_error = ?, finished_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?""",
            (status, run_after, error, finished_at, now_text, job_id, token),
        )
        assert cursor.rowcount == 1, "owned job changed during an immediate transaction"
        return status


async def cancel_job(db: aiosqlite.Connection, job_id: int, *, now: datetime | None = None) -> bool:
    now_text = _timestamp(_transition_time(now))
    async with _immediate_transaction(db):
        cursor = await db.execute(
            """UPDATE jobs
               SET status = 'cancelled', finished_at = ?, updated_at = ?
               WHERE id = ? AND status = 'queued'""",
            (now_text, now_text, job_id),
        )
        return cursor.rowcount == 1


async def retry_job(
    db: aiosqlite.Connection,
    job_id: int,
    expected_status: str,
    expected_attempts: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Explicitly authorize another attempt while preserving the attempt counter."""
    if expected_status not in _EXPLICIT_RETRY_STATUSES:
        raise ValueError("expected_status must be failed, cancelled, or needs_attention")
    if (
        not isinstance(expected_attempts, int)
        or isinstance(expected_attempts, bool)
        or not 0 <= expected_attempts <= 100
    ):
        raise ValueError("expected_attempts must be between 0 and 100")
    now_text = _timestamp(_transition_time(now))
    try:
        async with _immediate_transaction(db):
            cursor = await db.execute(
                """UPDATE jobs
                   SET status = 'queued',
                       max_attempts = CASE WHEN attempts >= max_attempts THEN max_attempts + 1 ELSE max_attempts END,
                       run_after = ?, locked_at = NULL, locked_by = NULL, claim_token = NULL,
                       last_error = '', result_json = '{}', finished_at = NULL, updated_at = ?
                   WHERE id = ? AND status = ? AND attempts = ?
                      AND (attempts < max_attempts OR max_attempts < 100)""",
                (now_text, now_text, job_id, expected_status, expected_attempts),
            )
            return cursor.rowcount == 1
    except sqlite3.IntegrityError:
        return False


async def recover_stale_jobs(
    db: aiosqlite.Connection,
    stale_before: datetime,
    *,
    now: datetime | None = None,
    retry_delay_seconds: int = 60,
    limit: int = 20,
) -> list[RecoveryResult]:
    """Recover a bounded stale set; paid/manual work always needs user attention."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= JOB_RECOVERY_LIMIT_MAX:
        raise ValueError(f"Recovery limit must be between 1 and {JOB_RECOVERY_LIMIT_MAX}")
    delay = _retry_delay(retry_delay_seconds)
    cutoff = _timestamp(stale_before)
    now_value = _transition_time(now)
    now_text = _timestamp(now_value)
    if cutoff > now_text:
        raise ValueError("stale_before cannot be later than now")
    retry_at = _timestamp(now_value + timedelta(seconds=delay))
    recovered: list[RecoveryResult] = []

    async with _immediate_transaction(db):
        rows = await db.execute_fetchall(
            """SELECT id, kind, payload_json, claim_token, attempts, max_attempts, retry_policy FROM jobs
               WHERE status = 'running' AND locked_at <= ?
               ORDER BY locked_at, id LIMIT ?""",
            (cutoff, limit),
        )
        for row in rows:
            if row["retry_policy"] == "manual":
                status = "needs_attention"
            elif row["attempts"] < row["max_attempts"]:
                has_successor = await _has_covering_queued_successor(db, row["kind"], row["payload_json"], now_text)
                status = "failed" if has_successor else "queued"
            else:
                status = "failed"
            finished_at = None if status == "queued" else now_text
            run_after = retry_at if status == "queued" else now_text
            cursor = await db.execute(
                """UPDATE jobs
                    SET status = ?, run_after = ?, locked_at = NULL, locked_by = NULL, claim_token = NULL,
                       last_error = 'Interrupted while running.', finished_at = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND claim_token = ? AND locked_at <= ?""",
                (status, run_after, finished_at, now_text, row["id"], row["claim_token"], cutoff),
            )
            assert cursor.rowcount == 1, "stale job changed during an immediate transaction"
            recovered.append(RecoveryResult(job_id=row["id"], status=status))
    return recovered


async def get_job(db: aiosqlite.Connection, job_id: int) -> dict[str, Any] | None:
    rows = await db.execute_fetchall("SELECT * FROM jobs WHERE id = ?", (job_id,))
    return dict(rows[0]) if rows else None


async def get_job_status(db: aiosqlite.Connection, job_id: int) -> dict[str, Any] | None:
    """Return only fields safe for the session-authenticated status UI."""
    rows = await db.execute_fetchall(
        """SELECT id, kind, status, attempts, max_attempts, last_error,
                  created_at, started_at, finished_at, result_json
           FROM jobs WHERE id = ?""",
        (job_id,),
    )
    return _job_status_projection(rows[0]) if rows else None


async def list_recent_job_statuses(db: aiosqlite.Connection, *, limit: int = 8) -> list[dict[str, Any]]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= JOB_STATUS_LIST_LIMIT_MAX:
        raise ValueError(f"Job status limit must be between 1 and {JOB_STATUS_LIST_LIMIT_MAX}")
    rows = await db.execute_fetchall(
        """SELECT id, kind, status, attempts, max_attempts, last_error,
                  created_at, started_at, finished_at, result_json
           FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?""",
        (limit,),
    )
    return [_job_status_projection(row) for row in rows]


async def prune_terminal_jobs(
    db: aiosqlite.Connection,
    kind: str,
    *,
    keep: int = JOB_TERMINAL_HISTORY_MAX,
) -> int:
    normalized_kind = _kind(kind)
    if not isinstance(keep, int) or isinstance(keep, bool) or not 1 <= keep <= JOB_TERMINAL_HISTORY_MAX:
        raise ValueError(f"Terminal job history must be between 1 and {JOB_TERMINAL_HISTORY_MAX}")
    async with _immediate_transaction(db):
        cursor = await db.execute(
            """DELETE FROM jobs
               WHERE kind = ? AND status IN ('succeeded', 'failed', 'cancelled', 'needs_attention')
                 AND id NOT IN (
                     SELECT id FROM jobs
                     WHERE kind = ? AND status IN ('succeeded', 'failed', 'cancelled', 'needs_attention')
                     ORDER BY id DESC LIMIT ?
                 )""",
            (normalized_kind, normalized_kind, keep),
        )
        return cursor.rowcount


def _job_status_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    projected = dict(row)
    raw_result = projected.pop("result_json", "{}")
    projected["outcome"] = ""
    if projected["kind"] == "oura_sync" and projected["status"] == "succeeded":
        try:
            result = json.loads(raw_result)
        except (TypeError, ValueError):
            result = {}
        if isinstance(result, dict) and result.get("complete") is False:
            projected["outcome"] = "partial"
    return projected


def decode_job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Stored job payload contains invalid constant: {value}")

    payload = json.loads(job["payload_json"], parse_constant=reject_constant)
    if not isinstance(payload, dict):
        raise ValueError("Stored job payload must be a JSON object")
    return payload
