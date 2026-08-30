"""A.N.D.Y. suggestions are a durable job with an explicit, manual retry."""

import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

import app.services.andy as andy_module
from app.services.job_handlers import handle_andy_generation
from app.services.job_worker import AmbiguousJobError, JobContext, VisibleJobError
from app.services.llm import LLMCallAmbiguousError
from app.services.llm_jobs import enqueue_paid_llm_job, llm_result_published, paid_llm_job_key
from tests.conftest import csrf_token, drain_jobs, user_db_path

_KIND = "andy_generation"
_DAY = "2026-08-12"
_FULL = json.dumps(
    {
        "andy_body_desc": "row 2k",
        "andy_spirit_desc": "ten minutes quiet",
        "andy_account_desc": "close the invoice",
        "andy_relations_desc": "call mum",
    }
)


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM daily_logs WHERE date = ?", (_DAY,))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(auth_client):
    _reset()
    yield
    _reset()


def _rows(sql: str, params: tuple = ()) -> list:
    db = sqlite3.connect(user_db_path())
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, params)]
    finally:
        db.close()


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(user_db_path())
    db.row_factory = aiosqlite.Row
    return db


async def _available(_db) -> bool:
    return True


def _queue(auth_client, nonce: str) -> int:
    token = csrf_token(auth_client, "/daily")
    response = auth_client.post(
        "/daily/generate-andy",
        data={"date": _DAY, "job_nonce": nonce, "_csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.headers.get("location")
    return int(parse_qs(urlsplit(response.headers["location"]).query)["job_id"][0])


def test_the_daily_page_carries_a_nonce_so_a_double_click_is_one_job(auth_client, monkeypatch):
    monkeypatch.setattr("app.routers.daily.llm_available", _available)
    monkeypatch.setattr("app.services.andy.call_llm", _stub(_FULL))
    page = auth_client.get(f"/daily/{_DAY}")
    assert 'name="job_nonce"' in page.text

    nonce = "d" * 32
    assert _queue(auth_client, nonce) == _queue(auth_client, nonce)
    assert _rows("SELECT COUNT(*) AS c FROM jobs WHERE kind = ?", (_KIND,))[0]["c"] == 1


def test_a_new_nonce_buys_a_new_generation_for_the_same_day(auth_client, monkeypatch):
    monkeypatch.setattr("app.routers.daily.llm_available", _available)
    monkeypatch.setattr("app.services.andy.call_llm", _stub(_FULL))

    first = _queue(auth_client, "e" * 32)
    drain_jobs()
    second = _queue(auth_client, "f" * 32)
    drain_jobs()

    assert first != second, "regenerating the same day is a deliberate second purchase"
    assert _rows("SELECT COUNT(*) AS c FROM llm_publications WHERE kind = ?", (_KIND,))[0]["c"] == 2
    log = _rows("SELECT andy_body_desc FROM daily_logs WHERE date = ?", (_DAY,))
    assert log and log[0]["andy_body_desc"] == "row 2k"


def _stub(raw: str):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return raw

    return fake_call_llm


def test_suggestions_and_their_marker_commit_together(monkeypatch):
    monkeypatch.setattr("app.services.andy.call_llm", _stub(_FULL))

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, _DAY, "a" * 32)
            queued = await enqueue_paid_llm_job(db, _KIND, {"day": _DAY, "key_part": "a" * 32}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            first = await handle_andy_generation(context, {"day": _DAY, "key_part": "a" * 32})
            second = await handle_andy_generation(context, {"day": _DAY, "key_part": "a" * 32})
            return first, second, await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    first, second, published = asyncio.run(scenario())
    assert first == {"published": True, "day": _DAY, "filled": 4}
    assert second == {"published": False, "reason": "already_published"}
    assert published is True
    stored = _rows("SELECT andy_spirit_desc FROM daily_logs WHERE date = ?", (_DAY,))
    assert stored[0]["andy_spirit_desc"] == "ten minutes quiet"


def test_an_empty_answer_is_a_visible_failure_not_four_blank_tasks(monkeypatch):
    monkeypatch.setattr("app.services.andy.call_llm", _stub('{"andy_body_desc": "   "}'))

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, _DAY, "b" * 32)
            queued = await enqueue_paid_llm_job(db, _KIND, {"day": _DAY, "key_part": "b" * 32}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(VisibleJobError, match="no suggestions"):
                await handle_andy_generation(context, {"day": _DAY, "key_part": "b" * 32})
            return await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT COUNT(*) AS c FROM daily_logs WHERE date = ?", (_DAY,))[0]["c"] == 0


def test_an_uncertain_provider_outcome_writes_nothing(monkeypatch):
    async def uncertain(db, system_prompt, user_prompt, **kwargs):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr("app.services.andy.call_llm", uncertain)

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, _DAY, "c" * 32)
            queued = await enqueue_paid_llm_job(db, _KIND, {"day": _DAY, "key_part": "c" * 32}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_andy_generation(context, {"day": _DAY, "key_part": "c" * 32})
            return await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT COUNT(*) AS c FROM daily_logs WHERE date = ?", (_DAY,))[0]["c"] == 0


def test_the_stored_value_is_bounded_so_one_answer_cannot_fill_the_column(monkeypatch):
    monkeypatch.setattr(
        "app.services.andy.call_llm",
        _stub(json.dumps({"andy_body_desc": "x" * 5000})),
    )

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, _DAY, "g" * 32)
            queued = await enqueue_paid_llm_job(db, _KIND, {"day": _DAY, "key_part": "g" * 32}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            await handle_andy_generation(context, {"day": _DAY, "key_part": "g" * 32})
        finally:
            await db.close()

    asyncio.run(scenario())
    stored = _rows("SELECT andy_body_desc FROM daily_logs WHERE date = ?", (_DAY,))[0]["andy_body_desc"]
    assert len(stored) == andy_module.ANDY_VALUE_MAX


def test_no_request_path_generates_suggestions():
    source = Path("app/routers/daily.py").read_text()
    assert "call_llm" not in source
    assert "generate_andy_suggestions" not in source
