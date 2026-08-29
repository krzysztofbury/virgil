"""Session-scoped HTML status and explicit retry routes for durable jobs."""

import hashlib
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.feedback import error_redirect, success_redirect
from app.main import templates
from app.services.jobs import get_job_status, retry_job
from app.user_db import get_user_db_from_request

router = APIRouter(prefix="/api/jobs")

_NO_STORE = {"Cache-Control": "no-store"}
_ACTIVE_STATUSES = {"queued", "running"}
_RETRYABLE_STATUSES = {"failed", "needs_attention"}
_STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Completed",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "needs_attention": "Needs review",
}
_STATUS_DESCRIPTIONS = {
    "queued": "Waiting for a worker.",
    "running": "Work is in progress.",
    "succeeded": "Completed successfully.",
    "failed": "The last attempt failed.",
    "cancelled": "This job was cancelled.",
    "needs_attention": "The previous attempt may have completed outside Virgil.",
}


def build_job_view(row: dict[str, Any]) -> dict[str, Any]:
    status = row["status"]
    if status not in _STATUS_LABELS:
        raise ValueError(f"Unsupported job status: {status}")
    version_source = "\0".join(
        str(row[field]) for field in ("status", "attempts", "max_attempts", "last_error", "finished_at")
    )
    version = hashlib.sha256(version_source.encode()).hexdigest()[:32]
    query = urlencode({"known_version": version})
    return {
        "id": row["id"],
        "kind_label": row["kind"].replace("_", " ").title(),
        "status": status,
        "status_label": _STATUS_LABELS[status],
        "description": _STATUS_DESCRIPTIONS[status],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "last_error": row["last_error"] if status in _RETRYABLE_STATUSES else "",
        "created_at": row["created_at"],
        "version": version,
        "polling": status in _ACTIVE_STATUSES,
        "retryable": status in _RETRYABLE_STATUSES,
        "ambiguous": status == "needs_attention",
        "poll_url": f"/api/jobs/{row['id']}?{query}",
        "retry_url": f"/api/jobs/{row['id']}/retry",
    }


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _render_job(request: Request, row: dict[str, Any], *, status_code: int = 200, headers=None) -> Response:
    response_headers = {**_NO_STORE, **(headers or {})}
    return templates.TemplateResponse(
        request,
        "partials/job_status.html",
        {"job": build_job_view(row)},
        status_code=status_code,
        headers=response_headers,
    )


def _not_found() -> HTMLResponse:
    return HTMLResponse("Job not found.", status_code=404, headers=_NO_STORE)


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_status(request: Request, job_id: int, known_version: str | None = Query(None, max_length=32)):
    if job_id < 1:
        return _not_found()
    db = get_user_db_from_request(request)
    row = await get_job_status(db, job_id)
    if row is None:
        return _not_found()
    if known_version == build_job_view(row)["version"]:
        return Response(status_code=204, headers=_NO_STORE)
    return _render_job(request, row)


@router.post("/{job_id}/retry", response_class=HTMLResponse)
async def retry_job_route(request: Request, job_id: int, confirm_ambiguous: str = Form("")):
    redirect_path = "/settings?tab=automation"
    db = get_user_db_from_request(request)
    row = await get_job_status(db, job_id) if job_id >= 1 else None
    if row is None:
        if _is_htmx(request):
            return _not_found()
        return error_redirect(request, redirect_path, "Job not found.")

    if row["status"] == "needs_attention" and confirm_ambiguous != "yes":
        message = "Confirm that you checked the external service before retrying this job."
        if _is_htmx(request):
            return _render_job(
                request,
                row,
                status_code=409,
                headers={"X-Feedback-Kind": "error", "X-Feedback-Message": message, "X-Feedback-Swap": "true"},
            )
        return error_redirect(request, redirect_path, message)

    if row["status"] not in _RETRYABLE_STATUSES or not await retry_job(db, job_id, row["status"], row["attempts"]):
        current = await get_job_status(db, job_id)
        message = "This job is no longer available for retry."
        if _is_htmx(request) and current is not None:
            return _render_job(
                request,
                current,
                status_code=409,
                headers={"X-Feedback-Kind": "error", "X-Feedback-Message": message, "X-Feedback-Swap": "true"},
            )
        return error_redirect(request, redirect_path, message)

    updated = await get_job_status(db, job_id)
    if updated is None:
        return _not_found()
    if _is_htmx(request):
        return _render_job(
            request,
            updated,
            headers={"X-Feedback-Kind": "success", "X-Feedback-Message": "Job queued for retry."},
        )
    return success_redirect(request, redirect_path, "Job queued for retry.")
