"""One bounded feedback contract for PRG and HTMX mutations."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response

MAX_FEEDBACK_LENGTH = 240


def _bounded_message(message: str) -> str:
    text = re.sub(r"\s+", " ", str(message)).strip()
    if not text:
        raise ValueError("Feedback message cannot be empty")
    if len(text) <= MAX_FEEDBACK_LENGTH:
        return text
    return text[: MAX_FEEDBACK_LENGTH - 3].rstrip() + "..."


def feedback_url(path: str, *, msg: str | None = None, err: str | None = None, clear_draft: str | None = None) -> str:
    """Add one safe outcome to a local redirect while preserving other query parameters."""
    if (msg is None) == (err is None):
        raise ValueError("Feedback requires exactly one of msg or err")
    if path.startswith("//") or "\\" in path:
        raise ValueError("Feedback redirects must use a local absolute path")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError("Feedback redirects must use a local absolute path")

    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in {"msg", "err"}
    ]
    query.append(("msg" if msg is not None else "err", _bounded_message(msg if msg is not None else err)))
    if clear_draft:
        query = [(key, value) for key, value in query if key != "clear_draft"]
        query.append(("clear_draft", clear_draft[:120]))
    return urlunsplit(("", "", parsed.path, urlencode(query), parsed.fragment))


def feedback_redirect(
    request: Request,
    path: str,
    *,
    msg: str | None = None,
    err: str | None = None,
    clear_draft: str | None = None,
) -> Response:
    if getattr(request, "headers", {}).get("HX-Request") == "true":
        target = feedback_url(path, msg=msg, err=err, clear_draft=clear_draft)
        headers = {"HX-Redirect": target}
        if clear_draft:
            headers["X-Draft-Clear"] = clear_draft[:120]
        return Response(status_code=200, headers=headers)
    target = feedback_url(path, msg=msg, err=err, clear_draft=clear_draft)
    return RedirectResponse(target, status_code=303)


def success_redirect(request: Request, path: str, message: str, *, clear_draft: str | None = None) -> Response:
    return feedback_redirect(request, path, msg=message, clear_draft=clear_draft)


def error_redirect(request: Request, path: str, message: str) -> Response:
    return feedback_redirect(request, path, err=message)
