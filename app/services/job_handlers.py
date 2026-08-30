"""Trusted production handlers for durable workload jobs."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.db import set_setting
from app.services.job_producers import EXPORT_SCOPES, EXPORT_SECTIONS, JOB_TRIGGERS
from app.services.llm_jobs import paid_llm_job_key, paid_llm_trigger, run_paid_llm_job

if TYPE_CHECKING:
    from app.services.job_worker import JobContext

logger = logging.getLogger(__name__)


def _exact_payload(payload: Mapping[str, Any], keys: set[str]) -> None:
    if set(payload) != keys:
        raise ValueError(f"Job payload must contain exactly: {', '.join(sorted(keys))}")


def _iso_day(value: Any) -> str:
    from datetime import date as calendar_date

    if not isinstance(value, str):
        raise ValueError("Stored job day must be an ISO date string")
    try:
        parsed = calendar_date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Stored job day is not a valid date") from exc
    if parsed.isoformat() != value:
        raise ValueError("Stored job day must be in canonical YYYY-MM-DD form")
    return value


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


async def handle_morning_briefing(context: "JobContext", payload: Mapping[str, Any]) -> Mapping[str, Any]:
    from app.services.briefing import generate_briefing_text, save_briefing

    _exact_payload(payload, {"day", "key_part", "trigger"})
    trigger = paid_llm_trigger(payload["trigger"])
    day = _iso_day(payload["day"])
    key = paid_llm_job_key("morning_briefing", trigger, str(payload["key_part"]))

    async def publish(content: str) -> Mapping[str, Any]:
        return {"day": day, "chars": await save_briefing(context.db, day, content)}

    result = await run_paid_llm_job(
        context,
        "morning_briefing",
        key,
        lambda: generate_briefing_text(context.db, day),
        publish,
    )
    # Only a stored briefing may close the day, or one provider outage would
    # silently skip the day entirely.
    if trigger == "scheduled":
        await set_setting(context.db, "briefing_last_day", day)
    return result


def _positive_id(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"Stored job {field} must be a positive integer")
    return value


async def handle_wod_parse(context: "JobContext", payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Parse one captured training note into library-constrained entries.

    A deterministic parse failure still publishes: the provider was already
    paid, and storing the empty result with its reason is what puts the confirm
    screen - and its manual-entry path - back in front of the user. Only an
    uncertain provider outcome withholds the write, because there the charge
    itself is in doubt.
    """
    import json
    from dataclasses import asdict

    from app.services.llm import LLMCallAmbiguousError
    from app.services.wod_parser import parse_wod

    _exact_payload(payload, {"session_id"})
    session_id = _positive_id(payload["session_id"], "session id")
    key = paid_llm_job_key("wod_parse", str(session_id))

    rows = await context.db.execute_fetchall(
        "SELECT notes FROM training_sessions WHERE id = ?",
        (session_id,),
    )
    if not rows or not (rows[0]["notes"] or "").strip():
        raise ValueError("The captured training note is gone")
    note = rows[0]["notes"]

    async def produce() -> dict[str, Any]:
        try:
            parsed = await parse_wod(context.db, note)
        except LLMCallAmbiguousError:
            raise
        except Exception as exc:
            logger.warning("WOD parse failed for session %s: %s", session_id, exc)
            return {"entries": [], "unmatched": [], "parse_error": str(exc), "dropped": 0}
        return {
            "entries": [asdict(entry) for entry in parsed.entries],
            "unmatched": parsed.unmatched,
            "parse_error": "",
            "dropped": parsed.dropped,
        }

    async def publish(parsed: dict[str, Any]) -> Mapping[str, Any]:
        cursor = await context.db.execute(
            "UPDATE training_sessions SET wod_parsed = ? WHERE id = ? AND wod_parsed IS NULL",
            (json.dumps(parsed), session_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("The training session already carries a parse result")
        return {
            "session_id": session_id,
            "entries": len(parsed["entries"]),
            "unmatched": len(parsed["unmatched"]),
            "parsed": not parsed["parse_error"],
        }

    return await run_paid_llm_job(context, "wod_parse", key, produce, publish)
