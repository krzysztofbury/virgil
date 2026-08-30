"""Shared mutation feedback must be bounded, accessible, and transport-aware."""

import ast
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.conftest import csrf_token, user_db_path


def test_mutation_handlers_do_not_return_silent_redirects():
    offenders = []
    methods = {"post", "put", "patch", "delete"}
    for path in Path("app/routers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)):
            is_mutation = any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in methods
                for decorator in function.decorator_list
            )
            if not is_mutation:
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                    continue
                called = node.value.func
                if isinstance(called, ast.Name) and called.id == "RedirectResponse":
                    offenders.append(f"{path.name}:{node.lineno}:{function.name}")
    assert offenders == [], f"Mutation handlers must use explicit feedback helpers: {offenders}"


def test_feedback_url_preserves_destination_query_and_bounds_message():
    from app.feedback import MAX_FEEDBACK_LENGTH, feedback_url

    target = feedback_url("/settings?tab=data&msg=stale", msg=" x\r\n" + "y" * 500)
    parsed = urlsplit(target)
    query = parse_qs(parsed.query)

    assert parsed.path == "/settings"
    assert query["tab"] == ["data"]
    assert len(query["msg"][0]) == MAX_FEEDBACK_LENGTH
    assert "\r" not in query["msg"][0]
    assert "\n" not in query["msg"][0]
    assert "err" not in query


def test_feedback_url_requires_exactly_one_outcome():
    from app.feedback import feedback_url

    with pytest.raises(ValueError, match="exactly one"):
        feedback_url("/oura")
    with pytest.raises(ValueError, match="exactly one"):
        feedback_url("/oura", msg="ok", err="bad")


@pytest.mark.parametrize("path", ["//evil.example/path", "////evil.example/path", r"/\\evil.example/path"])
def test_feedback_url_rejects_ambiguous_external_paths(path):
    from app.feedback import feedback_url

    with pytest.raises(ValueError, match="local absolute"):
        feedback_url(path, msg="ok")


def test_htmx_draft_clear_has_header_and_navigation_fallback():
    from app.feedback import success_redirect

    response = success_redirect(
        SimpleNamespace(headers={"HX-Request": "true"}),
        "/daily/2026-08-28",
        "Saved.",
        clear_draft="daily:2026-08-28",
    )

    assert response.status_code == 200
    assert response.headers["X-Draft-Clear"] == "daily:2026-08-28"
    assert parse_qs(urlsplit(response.headers["HX-Redirect"]).query)["clear_draft"] == ["daily:2026-08-28"]


def test_base_feedback_is_accessible_persistent_and_escaped(auth_client):
    oversized_error = "%3Cscript%3Ealert(1)%3C/script%3E" + "x" * 500
    response = auth_client.get(f"/oura?msg=Saved&err={oversized_error}")

    assert response.status_code == 200
    assert 'id="feedback-status"' in response.text
    assert 'role="status"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'id="feedback-error"' in response.text
    assert 'role="alert"' in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text
    assert "data-feedback-dismiss" in response.text
    feedback = response.text[response.text.index('id="mutation-feedback"') :]
    feedback = feedback[: feedback.index("</section>")]
    assert "Saved" not in feedback, "an error must take precedence over contradictory success feedback"
    assert "x" * 241 not in feedback, "the read boundary must enforce the same 240-character cap"


@pytest.fixture
def connected_oura(auth_client):
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM integrations WHERE provider = 'oura'")
        db.execute(
            "INSERT INTO integrations (provider, client_id, client_secret_enc, status) "
            "VALUES ('oura', 'client', 'secret', 'connected')"
        )
        db.commit()
    finally:
        db.close()
    yield
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM integrations WHERE provider = 'oura'")
        db.commit()
    finally:
        db.close()


def _job_nonce(html: str) -> str:
    match = re.search(r'name="job_nonce" value="([0-9a-f]{32})"', html)
    assert match
    return match.group(1)


def test_oura_sync_native_queues_with_visible_feedback(auth_client, connected_oura):
    page = auth_client.get("/oura")
    response = auth_client.post(
        "/oura/api-sync",
        data={"_csrf_token": csrf_token(auth_client, "/oura"), "job_nonce": _job_nonce(page.text)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.path == "/oura"
    assert parse_qs(target.query)["msg"] == ["Oura sync queued."]
    assert parse_qs(target.query)["job_id"]


def test_oura_sync_htmx_queues_with_hx_redirect(auth_client, connected_oura):
    page = auth_client.get("/oura")
    response = auth_client.post(
        "/oura/api-sync",
        data={"_csrf_token": csrf_token(auth_client, "/oura"), "job_nonce": _job_nonce(page.text)},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    target = urlsplit(response.headers["HX-Redirect"])
    assert target.path == "/oura"
    assert parse_qs(target.query)["msg"] == ["Oura sync queued."]
    assert parse_qs(target.query)["job_id"]
    assert response.text == ""


def test_shared_js_distinguishes_transport_and_http_failures(client):
    source = client.get("/static/js/app.js").text

    assert "htmx:sendError" in source
    assert "htmx:timeout" in source
    assert "htmx:responseError" in source
    assert "Network error. Your changes were not confirmed." in source
    assert "Request timed out. Your changes were not confirmed." in source
    assert "Server error. Your changes were not confirmed." in source
    assert "sessionStorage" in source
    assert "data-draft-fields" in source
    assert "HTMX_TIMEOUT_MS = 30000" in source
    assert "DRAFT_COUNT_MAX = 12" in source
    assert "event.defaultPrevented" in source
    assert "form._feedbackSubmitter = control" in source


def test_mutation_forms_declare_pending_and_bounded_draft_contract(auth_client):
    oura = Path("app/templates/oura.html").read_text(encoding="utf-8")
    daily = auth_client.get("/daily").text
    feniks = Path("app/templates/feniks.html").read_text(encoding="utf-8")

    assert 'data-pending-label="Syncing..."' in oura
    assert 'data-draft-fields="notes"' in daily
    assert 'hx-sync="this:replace"' in daily
    assert 'data-draft-fields="note"' in feniks
    assert 'data-draft-fields="hook,story"' in feniks
