"""The morning briefing is a durable job, not work done inside a request."""

import asyncio
import re
import sqlite3
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

import app.services.briefing as briefing_module
from app.services.job_handlers import handle_morning_briefing
from app.services.job_worker import AmbiguousJobError, JobContext
from app.services.llm import LLMCallAmbiguousError
from app.services.llm_jobs import enqueue_paid_llm_job, llm_result_published, paid_llm_job_key
from tests.conftest import csrf_token, user_db_path

_KIND = "morning_briefing"
_TODAY = date.today().isoformat()


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM daily_briefings")
        db.execute("DELETE FROM app_settings WHERE key LIKE 'briefing_%'")
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(auth_client):
    _reset()
    yield
    _reset()


def _enable_briefing() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('briefing_enabled', '1')")
        db.commit()
    finally:
        db.close()


def _nonce(html: str) -> str:
    match = re.search(r'name="job_nonce" value="([0-9a-f]{32})"', html)
    assert match, "the briefing form must carry a nonce so a double submit is one job"
    return match.group(1)


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(user_db_path())
    db.row_factory = aiosqlite.Row
    return db


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    db = sqlite3.connect(user_db_path())
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def test_no_request_path_still_calls_the_provider_for_a_briefing():
    """The route may only persist intent; the charge belongs to the worker."""
    source = Path("app/routers/dashboard.py").read_text()
    assert "generate_briefing_text" not in source
    assert "call_llm" not in source
    scheduler = Path("app/services/scheduler.py").read_text()
    assert "generate_briefing" not in scheduler, "a 60-second tick must not wait on a provider"


def test_generating_queues_one_job_and_a_double_submit_reuses_it(auth_client, monkeypatch):
    _enable_briefing()
    monkeypatch.setattr("app.services.llm.llm_available", _always_available)
    page = auth_client.get("/")
    nonce = _nonce(page.text)
    token = csrf_token(auth_client, "/")

    responses = [
        auth_client.post(
            "/api/briefing/generate",
            data={"job_nonce": nonce, "_csrf_token": token},
            follow_redirects=False,
        )
        for _ in range(2)
    ]

    job_ids = set()
    for response in responses:
        assert response.status_code == 303
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["msg"] == ["Briefing queued."]
        job_ids.add(int(query["job_id"][0]))
    assert len(job_ids) == 1
    kind, attempts, policy = _rows("SELECT kind, max_attempts, retry_policy FROM jobs")[0]
    assert (kind, attempts, policy) == (_KIND, 1, "manual"), "a paid job never retries itself"


def test_a_missing_provider_is_refused_before_a_job_exists(auth_client):
    _enable_briefing()
    page = auth_client.get("/")
    response = auth_client.post(
        "/api/briefing/generate",
        data={"job_nonce": _nonce(page.text), "_csrf_token": csrf_token(auth_client, "/")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "err=" in response.headers["location"]
    assert _rows("SELECT COUNT(*) FROM jobs")[0][0] == 0


async def _always_available(_db) -> bool:
    return True


def test_the_handler_publishes_the_briefing_and_its_marker_together(monkeypatch):
    calls = []

    async def fake_text(db, day_iso):
        calls.append(day_iso)
        return "  # Good morning\n\nStay steady.  "

    monkeypatch.setattr(briefing_module, "generate_briefing_text", fake_text)

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, "manual", "a" * 32)
            queued = await enqueue_paid_llm_job(
                db, _KIND, {"day": _TODAY, "trigger": "manual", "key_part": "a" * 32}, idempotency_key=key
            )
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            first = await handle_morning_briefing(context, {"day": _TODAY, "trigger": "manual", "key_part": "a" * 32})
            # A replayed attempt after a crash must not buy the answer twice.
            second = await handle_morning_briefing(context, {"day": _TODAY, "trigger": "manual", "key_part": "a" * 32})
            return first, second, await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    first, second, published = asyncio.run(scenario())

    assert first["published"] is True
    assert second == {"published": False, "reason": "already_published"}
    assert calls == [_TODAY], "the provider must be called exactly once"
    assert published is True
    assert _rows("SELECT content FROM daily_briefings WHERE date = ?", (_TODAY,)) == [
        ("# Good morning\n\nStay steady.",)
    ]


def test_a_failed_publish_leaves_no_briefing_and_no_marker(monkeypatch):
    async def fake_text(db, day_iso):
        return "text that will not land"

    async def exploding_save(db, day_iso, content):
        raise RuntimeError("disk gave up")

    monkeypatch.setattr(briefing_module, "generate_briefing_text", fake_text)
    monkeypatch.setattr(briefing_module, "save_briefing", exploding_save)

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, "manual", "b" * 32)
            queued = await enqueue_paid_llm_job(
                db, _KIND, {"day": _TODAY, "trigger": "manual", "key_part": "b" * 32}, idempotency_key=key
            )
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(RuntimeError):
                await handle_morning_briefing(context, {"day": _TODAY, "trigger": "manual", "key_part": "b" * 32})
            assert db.in_transaction is False, "a failed publish must not leave the writer locked"
            return await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT COUNT(*) FROM daily_briefings")[0][0] == 0


def test_an_uncertain_provider_outcome_asks_for_review_instead_of_retrying(monkeypatch):
    async def ambiguous(db, day_iso):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr(briefing_module, "generate_briefing_text", ambiguous)

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, "manual", "c" * 32)
            queued = await enqueue_paid_llm_job(
                db, _KIND, {"day": _TODAY, "trigger": "manual", "key_part": "c" * 32}, idempotency_key=key
            )
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_morning_briefing(context, {"day": _TODAY, "trigger": "manual", "key_part": "c" * 32})
            return await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT COUNT(*) FROM daily_briefings")[0][0] == 0


def test_only_a_stored_scheduled_briefing_closes_the_day(monkeypatch):
    async def fake_text(db, day_iso):
        return "scheduled briefing"

    monkeypatch.setattr(briefing_module, "generate_briefing_text", fake_text)

    async def scenario():
        db = await _db()
        try:
            payload = {"day": _TODAY, "trigger": "scheduled", "key_part": _TODAY}
            queued = await enqueue_paid_llm_job(
                db, _KIND, payload, idempotency_key=paid_llm_job_key(_KIND, "scheduled", _TODAY)
            )
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            await handle_morning_briefing(context, payload)
        finally:
            await db.close()

    asyncio.run(scenario())
    assert _rows("SELECT value FROM app_settings WHERE key = 'briefing_last_day'") == [(_TODAY,)]


def test_a_scheduled_failure_does_not_close_the_day(monkeypatch):
    async def ambiguous(db, day_iso):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr(briefing_module, "generate_briefing_text", ambiguous)

    async def scenario():
        db = await _db()
        try:
            payload = {"day": _TODAY, "trigger": "scheduled", "key_part": _TODAY}
            queued = await enqueue_paid_llm_job(
                db, _KIND, payload, idempotency_key=paid_llm_job_key(_KIND, "scheduled", _TODAY)
            )
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_morning_briefing(context, payload)
        finally:
            await db.close()

    asyncio.run(scenario())
    assert _rows("SELECT COUNT(*) FROM app_settings WHERE key = 'briefing_last_day'")[0][0] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"day": "not-a-date", "trigger": "manual", "key_part": "a" * 32},
        {"day": "20260830", "trigger": "manual", "key_part": "a" * 32},
        {"day": _TODAY, "trigger": "webhook", "key_part": "a" * 32},
        {"day": _TODAY, "trigger": "manual"},
        {"day": _TODAY, "trigger": "manual", "key_part": "a" * 32, "extra": 1},
    ],
)
def test_a_stored_payload_is_validated_before_a_provider_is_touched(monkeypatch, payload):
    async def must_not_run(db, day_iso):
        raise AssertionError("the provider must never see an invalid payload")

    monkeypatch.setattr(briefing_module, "generate_briefing_text", must_not_run)

    async def scenario():
        db = await _db()
        try:
            context = JobContext(db=db, user_id="user", job_id=1, attempt=1)
            with pytest.raises(ValueError):
                await handle_morning_briefing(context, payload)
        finally:
            await db.close()

    asyncio.run(scenario())
