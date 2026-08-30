"""Experiment summaries: no paid work on a GET, one purchase per week."""

import asyncio
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

from app.services.experiment_summary import SUMMARY_JOB_KIND, due_summary_weeks, enqueue_due_summary
from app.services.job_handlers import handle_experiment_summary
from app.services.job_worker import AmbiguousJobError, JobContext
from app.services.llm import LLMCallAmbiguousError
from app.services.llm_jobs import enqueue_paid_llm_job, llm_result_published, paid_llm_job_key
from tests.conftest import csrf_token, drain_jobs, user_db_path


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM experiment_summaries")
        db.execute(
            "DELETE FROM experiment_weeks WHERE experiment_id IN (SELECT id FROM experiments WHERE title LIKE 'ES %')"
        )
        db.execute("DELETE FROM experiments WHERE title LIKE 'ES %'")
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


def _seed_experiment(weeks: int = 3) -> int:
    """An experiment whose first two weeks are already over."""
    start = date.today() - timedelta(weeks=weeks)
    start -= timedelta(days=start.weekday())
    db = sqlite3.connect(user_db_path())
    try:
        cursor = db.execute(
            "INSERT INTO experiments (title, description, start_date, num_weeks, status) "
            "VALUES ('ES probe', 'probe', ?, ?, 'active')",
            (start.isoformat(), weeks),
        )
        experiment_id = int(cursor.lastrowid)
        for week_number in range(1, weeks + 1):
            db.execute(
                "INSERT INTO experiment_weeks (experiment_id, week_number) VALUES (?, ?)",
                (experiment_id, week_number),
            )
        db.commit()
        return experiment_id
    finally:
        db.close()


def _stub(monkeypatch, text: str = "week went well"):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return text

    monkeypatch.setattr("app.services.experiment_summary.call_llm", fake_call_llm)


async def _available(_db) -> bool:
    return True


def test_opening_the_detail_page_never_spends_money(auth_client, monkeypatch):
    experiment_id = _seed_experiment()
    calls = []

    async def must_not_run(db, system_prompt, user_prompt, **kwargs):
        calls.append(user_prompt)
        return "nope"

    monkeypatch.setattr("app.services.experiment_summary.call_llm", must_not_run)
    response = auth_client.get(f"/experiments/{experiment_id}")

    assert response.status_code == 200
    assert calls == []
    assert _rows("SELECT COUNT(*) AS c FROM jobs")[0]["c"] == 0, "a page load is not a schedule"
    source = Path("app/routers/experiments.py").read_text()
    assert "call_llm" not in source


def test_the_scheduler_queues_one_missing_week_at_a_time(monkeypatch):
    experiment_id = _seed_experiment()
    monkeypatch.setattr("app.services.experiment_summary.has_llm", _available)

    async def scenario():
        db = await _db()
        try:
            first = await enqueue_due_summary(db, experiment_id)
            # Migration 029 allows one queued paid job per kind, so the next
            # week has to wait rather than filling the queue.
            second = await enqueue_due_summary(db, experiment_id)
            return first, second, await due_summary_weeks(db, experiment_id)
        finally:
            await db.close()

    first, second, due = asyncio.run(scenario())
    assert first is not None
    assert second is None
    assert due == [1, 2, 3]
    payload = _rows("SELECT payload_json FROM jobs WHERE kind = ?", (SUMMARY_JOB_KIND,))
    assert len(payload) == 1
    assert '"week_number":1' in payload[0]["payload_json"]


def test_a_queued_week_is_never_queued_again(monkeypatch):
    experiment_id = _seed_experiment()
    monkeypatch.setattr("app.services.experiment_summary.has_llm", _available)
    _stub(monkeypatch)

    async def scenario():
        db = await _db()
        try:
            await enqueue_due_summary(db, experiment_id)
        finally:
            await db.close()

    asyncio.run(scenario())
    drain_jobs()
    asyncio.run(scenario())

    weeks = [row["week_number"] for row in _rows("SELECT week_number FROM experiment_summaries ORDER BY week_number")]
    assert weeks == [1]
    queued = _rows("SELECT payload_json FROM jobs WHERE kind = ? AND status = 'queued'", (SUMMARY_JOB_KIND,))
    assert len(queued) == 1
    assert '"week_number":2' in queued[0]["payload_json"], "the next missing week moves up, week 1 does not repeat"


def test_manual_generation_queues_and_never_double_buys(auth_client, monkeypatch):
    experiment_id = _seed_experiment()
    monkeypatch.setattr("app.services.experiment_summary.has_llm", _available)
    _stub(monkeypatch)
    token = csrf_token(auth_client, f"/experiments/{experiment_id}")

    def post():
        return auth_client.post(
            f"/experiments/{experiment_id}/generate-summary",
            data={"week_number": "2", "_csrf_token": token},
            follow_redirects=False,
        )

    first, second = post(), post()
    assert first.status_code == second.status_code == 303
    first_id = int(parse_qs(urlsplit(first.headers["location"]).query)["job_id"][0])
    second_id = int(parse_qs(urlsplit(second.headers["location"]).query)["job_id"][0])
    assert first_id == second_id

    drain_jobs()
    stored = _rows("SELECT week_number, summary FROM experiment_summaries")
    assert stored == [{"week_number": 2, "summary": "week went well"}]


def test_the_summary_and_its_marker_commit_together(monkeypatch):
    experiment_id = _seed_experiment()
    _stub(monkeypatch, "  detailed week  ")
    payload = {"experiment_id": experiment_id, "week_number": 1, "trigger": "manual"}
    key = paid_llm_job_key(SUMMARY_JOB_KIND, str(experiment_id), "1", "manual")

    async def scenario():
        db = await _db()
        try:
            queued = await enqueue_paid_llm_job(db, SUMMARY_JOB_KIND, payload, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            first = await handle_experiment_summary(context, payload)
            second = await handle_experiment_summary(context, payload)
            return first, second, await llm_result_published(db, SUMMARY_JOB_KIND, key)
        finally:
            await db.close()

    first, second, published = asyncio.run(scenario())
    assert first == {"published": True, "experiment_id": experiment_id, "week_number": 1, "chars": 13}
    assert second == {"published": False, "reason": "already_published"}
    assert published is True
    assert _rows("SELECT summary FROM experiment_summaries")[0]["summary"] == "detailed week"


def test_an_uncertain_provider_outcome_stores_nothing(monkeypatch):
    experiment_id = _seed_experiment()

    async def uncertain(db, system_prompt, user_prompt, **kwargs):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr("app.services.experiment_summary.call_llm", uncertain)
    payload = {"experiment_id": experiment_id, "week_number": 1, "trigger": "manual"}
    key = paid_llm_job_key(SUMMARY_JOB_KIND, str(experiment_id), "1", "manual")

    async def scenario():
        db = await _db()
        try:
            queued = await enqueue_paid_llm_job(db, SUMMARY_JOB_KIND, payload, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_experiment_summary(context, payload)
            return await llm_result_published(db, SUMMARY_JOB_KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT COUNT(*) AS c FROM experiment_summaries")[0]["c"] == 0


def test_the_cooldown_dictionary_is_gone():
    """It was process-local and shared between users: neither restart-safe nor scoped."""
    source = Path("app/services/experiment_summary.py").read_text()
    assert "_last_attempt" not in source
    assert "_COOLDOWN_SECONDS" not in source


@pytest.mark.parametrize(
    "payload",
    [
        {"experiment_id": 1, "week_number": 0, "trigger": "manual"},
        {"experiment_id": 1, "week_number": 1, "trigger": "webhook"},
        {"experiment_id": 1, "week_number": 1},
    ],
)
def test_a_stored_summary_payload_is_validated(payload, monkeypatch):
    async def must_not_run(db, system_prompt, user_prompt, **kwargs):
        raise AssertionError("no provider call for an invalid payload")

    monkeypatch.setattr("app.services.experiment_summary.call_llm", must_not_run)

    async def scenario():
        db = await _db()
        try:
            context = JobContext(db=db, user_id="user", job_id=1, attempt=1)
            with pytest.raises(ValueError):
                await handle_experiment_summary(context, payload)
        finally:
            await db.close()

    asyncio.run(scenario())
