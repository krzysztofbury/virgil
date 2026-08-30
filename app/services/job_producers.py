"""Canonical producers for durable backup, export, and Oura jobs."""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

import aiosqlite

from app.services.jobs import JOB_TERMINAL_HISTORY_MAX, EnqueueResult, enqueue_job, prune_terminal_jobs

BACKUP_JOB_KIND = "backup"
MARKDOWN_EXPORT_JOB_KIND = "markdown_export"
OURA_SYNC_JOB_KIND = "oura_sync"
WORKLOAD_JOB_KINDS = frozenset({BACKUP_JOB_KIND, MARKDOWN_EXPORT_JOB_KIND, OURA_SYNC_JOB_KIND})
WORKLOAD_JOB_MAX_ATTEMPTS = 3
EXPORT_SCOPES = frozenset({"weekly", "monthly", "yearly", "all"})
EXPORT_SECTIONS = frozenset(
    {
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
)
JOB_TRIGGERS = frozenset({"manual", "scheduled", "webhook"})
_MANUAL_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
WEBHOOK_TIMESTAMP_SKEW_SECONDS = 300
_TRIGGER_PRIORITY = {"webhook": 0, "manual": 1, "scheduled": 2}


class ActiveWorkloadConflictError(RuntimeError):
    """A queued workload cannot safely satisfy different requested work."""


def manual_job_key(kind: str, nonce: str) -> str:
    if kind not in WORKLOAD_JOB_KINDS:
        raise ValueError("Unsupported workload job kind")
    if not _MANUAL_NONCE_RE.fullmatch(nonce or ""):
        raise ValueError("Manual job nonce must be 32 lowercase hexadecimal characters")
    return f"{kind}:manual:{nonce}"


def scheduled_job_key(kind: str, interval_hours: float, *, now: datetime | None = None) -> str:
    if kind not in WORKLOAD_JOB_KINDS:
        raise ValueError("Unsupported workload job kind")
    if not isinstance(interval_hours, (int, float)) or isinstance(interval_hours, bool):
        raise ValueError("Schedule interval must be numeric")
    if not math.isfinite(interval_hours) or not 1 <= interval_hours <= 168:
        raise ValueError("Schedule interval must be between 1 and 168 hours")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Schedule time must be timezone-aware")
    interval_seconds = round(interval_hours * 3600)
    slot = int(current.astimezone(UTC).timestamp()) // interval_seconds
    return f"{kind}:scheduled:{interval_seconds}:{slot}"


def webhook_oura_job_key(timestamp: str, body: bytes) -> str:
    if not timestamp or not isinstance(body, bytes) or not body:
        raise ValueError("Webhook delivery identity requires timestamp and body bytes")
    digest = hashlib.sha256(timestamp.encode() + b"\0" + body).hexdigest()
    return f"{OURA_SYNC_JOB_KIND}:webhook:{digest}"


def _trigger(value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        raise ValueError(f"Job trigger must be one of: {', '.join(sorted(allowed))}")
    return value


async def _queued_workload(db: aiosqlite.Connection, kind: str) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT id, payload_json FROM jobs WHERE kind = ? AND status = 'queued' ORDER BY id LIMIT 1",
        (kind,),
    )
    return dict(rows[0]) if rows else None


async def _running_workload(db: aiosqlite.Connection, kind: str) -> dict | None:
    rows = await db.execute_fetchall(
        "SELECT id, payload_json FROM jobs WHERE kind = ? AND status = 'running' ORDER BY id LIMIT 1",
        (kind,),
    )
    return dict(rows[0]) if rows else None


async def _existing_owned_key(db: aiosqlite.Connection, kind: str, idempotency_key: str) -> EnqueueResult | None:
    if kind == MARKDOWN_EXPORT_JOB_KIND:
        return None
    rows = await db.execute_fetchall("SELECT id, kind FROM jobs WHERE idempotency_key = ?", (idempotency_key,))
    if not rows:
        return None
    if rows[0]["kind"] != kind:
        raise ActiveWorkloadConflictError("Workload idempotency key belongs to another job kind")
    return EnqueueResult(job_id=rows[0]["id"], created=False)


def _payload_object(payload_json: str) -> dict:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError) as exc:
        raise ActiveWorkloadConflictError("Running workload payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ActiveWorkloadConflictError("Running workload payload is invalid")
    return payload


def _merged_trigger(first: object, second: object) -> str:
    if first not in _TRIGGER_PRIORITY or second not in _TRIGGER_PRIORITY:
        raise ActiveWorkloadConflictError("Running workload trigger is invalid")
    return max((first, second), key=_TRIGGER_PRIORITY.__getitem__)


def _successor_payload(kind: str, running_payload_json: str, requested: dict) -> dict:
    running = _payload_object(running_payload_json)
    if kind == BACKUP_JOB_KIND:
        return {"trigger": _merged_trigger(running.get("trigger"), requested.get("trigger"))}
    if kind == OURA_SYNC_JOB_KIND:
        running_days = running.get("days_back")
        requested_days = requested.get("days_back")
        if not isinstance(running_days, int) or isinstance(running_days, bool) or not 1 <= running_days <= 30:
            raise ActiveWorkloadConflictError("Running Oura sync range is invalid")
        return {
            "days_back": max(running_days, requested_days),
            "trigger": _merged_trigger(running.get("trigger"), requested.get("trigger")),
        }
    if running != requested:
        raise ActiveWorkloadConflictError("Running markdown export does not cover the requested export")
    return requested


def _queued_job_satisfies(kind: str, existing_payload_json: str, requested_payload: dict) -> bool:
    try:
        existing = json.loads(existing_payload_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(existing, dict):
        return False
    if requested_payload.get("trigger") == "scheduled" and existing.get("trigger") != "scheduled":
        return False
    if existing == requested_payload or kind == BACKUP_JOB_KIND:
        return True
    if kind == OURA_SYNC_JOB_KIND:
        existing_days = existing.get("days_back")
        requested_days = requested_payload.get("days_back")
        return isinstance(existing_days, int) and isinstance(requested_days, int) and existing_days >= requested_days
    return False


async def _coalesced_result(db: aiosqlite.Connection, kind: str, payload: dict) -> EnqueueResult | None:
    active = await _queued_workload(db, kind)
    if active is None:
        return None
    if not _queued_job_satisfies(kind, active["payload_json"], payload):
        raise ActiveWorkloadConflictError(f"Another {kind} job is already queued")
    return EnqueueResult(job_id=active["id"], created=False)


async def _enqueue_workload(
    db: aiosqlite.Connection,
    kind: str,
    payload: dict,
    *,
    idempotency_key: str,
) -> EnqueueResult:
    existing = await _existing_owned_key(db, kind, idempotency_key)
    if existing is not None:
        return existing
    active = await _coalesced_result(db, kind, payload)
    if active is not None:
        return active
    running = await _running_workload(db, kind)
    successor_payload = _successor_payload(kind, running["payload_json"], payload) if running else payload
    try:
        result = await enqueue_job(
            db,
            kind,
            successor_payload,
            idempotency_key=idempotency_key,
            max_attempts=WORKLOAD_JOB_MAX_ATTEMPTS,
            retry_policy="automatic",
        )
    except sqlite3.IntegrityError:
        active = await _coalesced_result(db, kind, payload)
        if active is None:
            raise
        return active
    if result.created:
        await prune_terminal_jobs(db, kind, keep=JOB_TERMINAL_HISTORY_MAX - 1)
    return result


async def enqueue_backup_job(
    db: aiosqlite.Connection,
    *,
    trigger: str,
    idempotency_key: str,
) -> EnqueueResult:
    normalized_trigger = _trigger(trigger, frozenset({"manual", "scheduled"}))
    return await _enqueue_workload(
        db,
        BACKUP_JOB_KIND,
        {"trigger": normalized_trigger},
        idempotency_key=idempotency_key,
    )


async def enqueue_markdown_export_job(
    db: aiosqlite.Connection,
    *,
    trigger: str,
    scope: str,
    sections: Iterable[str] | None,
    idempotency_key: str,
) -> EnqueueResult:
    normalized_trigger = _trigger(trigger, frozenset({"manual", "scheduled"}))
    if scope not in EXPORT_SCOPES:
        raise ValueError("Unsupported export scope")
    normalized_sections = None
    if sections is not None:
        if isinstance(sections, (str, bytes)):
            raise ValueError("Export sections must be a collection")
        values = list(sections)
        if not values or len(values) > len(EXPORT_SECTIONS) or any(value not in EXPORT_SECTIONS for value in values):
            raise ValueError("Export sections must contain known section names")
        normalized_sections = sorted(set(values))
    return await _enqueue_workload(
        db,
        MARKDOWN_EXPORT_JOB_KIND,
        {"scope": scope, "sections": normalized_sections, "trigger": normalized_trigger},
        idempotency_key=idempotency_key,
    )


async def enqueue_oura_sync_job(
    db: aiosqlite.Connection,
    *,
    trigger: str,
    days_back: int,
    idempotency_key: str,
) -> EnqueueResult:
    normalized_trigger = _trigger(trigger, frozenset({"manual", "scheduled", "webhook"}))
    if not isinstance(days_back, int) or isinstance(days_back, bool) or not 1 <= days_back <= 30:
        raise ValueError("Oura sync days_back must be between 1 and 30")
    return await _enqueue_workload(
        db,
        OURA_SYNC_JOB_KIND,
        {"days_back": days_back, "trigger": normalized_trigger},
        idempotency_key=idempotency_key,
    )
