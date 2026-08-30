"""Trusted production handlers for durable workload jobs."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.db import set_setting
from app.services.job_producers import EXPORT_SCOPES, EXPORT_SECTIONS, JOB_TRIGGERS

if TYPE_CHECKING:
    from app.services.job_worker import JobContext


def _exact_payload(payload: Mapping[str, Any], keys: set[str]) -> None:
    if set(payload) != keys:
        raise ValueError(f"Job payload must contain exactly: {', '.join(sorted(keys))}")


def _trigger(payload: Mapping[str, Any]) -> str:
    trigger = payload["trigger"]
    if not isinstance(trigger, str) or trigger not in JOB_TRIGGERS:
        raise ValueError("Stored job trigger is invalid")
    return trigger


async def handle_backup(context: "JobContext", payload: Mapping[str, Any]) -> Mapping[str, Any]:
    from app.services.backup import run_backup

    _exact_payload(payload, {"trigger"})
    trigger = _trigger(payload)
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("Backup trigger is invalid")
    rows = await context.db.execute_fetchall(
        "SELECT strftime('%Y-%m-%dT%H%M', created_at) || '-j' || printf('%020d', id) AS artifact_timestamp "
        "FROM jobs WHERE id = ?",
        (context.job_id,),
    )
    if len(rows) != 1 or not rows[0]["artifact_timestamp"]:
        raise RuntimeError("Backup job creation time is unavailable")
    path = await run_backup(context.db, artifact_timestamp=rows[0]["artifact_timestamp"])
    if trigger == "scheduled":
        await set_setting(context.db, "backup_last_run", datetime.now(UTC).isoformat())
    return {"filename": path.name}


async def handle_markdown_export(context: "JobContext", payload: Mapping[str, Any]) -> Mapping[str, Any]:
    from app.services.markdown_export import export_filename_for, write_export

    _exact_payload(payload, {"scope", "sections", "trigger"})
    trigger = _trigger(payload)
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("Markdown export trigger is invalid")
    scope = payload["scope"]
    if not isinstance(scope, str) or scope not in EXPORT_SCOPES:
        raise ValueError("Stored export scope is invalid")
    raw_sections = payload["sections"]
    sections = None
    if raw_sections is not None:
        if not isinstance(raw_sections, list) or not raw_sections:
            raise ValueError("Stored export sections are invalid")
        if len(raw_sections) > len(EXPORT_SECTIONS) or any(
            not isinstance(section, str) or section not in EXPORT_SECTIONS for section in raw_sections
        ):
            raise ValueError("Stored export sections are invalid")
        if raw_sections != sorted(set(raw_sections)):
            raise ValueError("Stored export sections are not canonical")
        sections = set(raw_sections)
    filename = await export_filename_for(context.db, context.user_id)
    content = await write_export(context.db, scope=scope, sections=sections, filename=filename)
    if trigger == "scheduled":
        await set_setting(context.db, "export_last_run", datetime.now(UTC).isoformat())
    return {"bytes": len(content.encode("utf-8")), "filename": filename, "scope": scope}


async def handle_oura_sync(context: "JobContext", payload: Mapping[str, Any]) -> Mapping[str, Any]:
    from app.services.oura_api import sync_oura_from_api

    _exact_payload(payload, {"days_back", "trigger"})
    trigger = _trigger(payload)
    days_back = payload["days_back"]
    if not isinstance(days_back, int) or isinstance(days_back, bool) or not 1 <= days_back <= 30:
        raise ValueError("Stored Oura sync range is invalid")
    result = await sync_oura_from_api(context.db, days_back=days_back)
    if trigger == "scheduled":
        await set_setting(context.db, "oura_sync_last_run", datetime.now(UTC).isoformat())
    return {
        "complete": result.complete,
        "days": result.days,
        "failed_daily_endpoints": list(result.failed_daily_endpoints),
        "workouts_synced": result.workouts_synced,
    }
