"""The scheduler is what actually runs a paid job in production.

Every other test in this branch calls the worker directly, so a break between
scheduler_tick and the handler registry would ship green. This is the one test
that walks the whole path.
"""

import asyncio
import sqlite3

import pytest

import app.services.wod_parser as wod_parser
from app.services.wod_parser import ParsedWod
from tests.conftest import csrf_token, user_db_path


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM training_sessions WHERE notes LIKE 'SCHED %'")
        db.execute("DELETE FROM daily_logs WHERE date = '2026-08-30'")
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(auth_client):
    _reset()
    yield
    _reset()


def test_a_scheduler_tick_claims_and_finishes_a_queued_paid_job(auth_client, monkeypatch):
    db = sqlite3.connect(user_db_path())
    try:
        session_id = db.execute(
            "INSERT INTO training_sessions (date, notes) VALUES ('2026-08-26', 'SCHED note')"
        ).lastrowid
        db.execute(
            """INSERT INTO jobs (kind, payload_json, status, idempotency_key, attempts, max_attempts,
                                 retry_policy, run_after, last_error, result_json, created_at, updated_at)
               VALUES ('wod_parse', ?, 'queued', ?, 0, 1, 'manual', datetime('now'), '', '{}',
                       datetime('now'), datetime('now'))""",
            (f'{{"session_id": {session_id}}}', f"wod_parse:{session_id}"),
        )
        db.commit()
    finally:
        db.close()

    async def fake_parse(_db, text):
        return ParsedWod(entries=[], unmatched=["Thruster"], dropped=0)

    monkeypatch.setattr(wod_parser, "parse_wod", fake_parse)

    from app.services.scheduler import scheduler_tick

    asyncio.run(scheduler_tick())

    db = sqlite3.connect(user_db_path())
    try:
        status = db.execute("SELECT status, last_error FROM jobs WHERE kind = 'wod_parse'").fetchone()
        parsed = db.execute(
            "SELECT wod_parsed IS NOT NULL FROM training_sessions WHERE id = ?", (session_id,)
        ).fetchone()
    finally:
        db.close()

    assert status == ("succeeded", ""), f"the tick left the job at {status}"
    assert parsed == (1,), "the handler must have published through the scheduler path"


def test_enqueueing_starts_the_work_now_instead_of_at_the_next_tick(auth_client, monkeypatch):
    """A user-initiated job that sits for up to TICK_SECONDS reads as broken.

    Enqueueing wakes a bounded worker pass, so the click is answered at once and
    the work starts within milliseconds. The scheduler stays the safety net.
    """
    import app.services.andy as andy_module

    monkeypatch.setattr("app.config.WORKER_WAKE", True)
    monkeypatch.setattr("app.routers.daily.llm_available", _always_available)
    monkeypatch.setattr(
        andy_module,
        "call_llm",
        _answer(
            '{"andy_body_desc": "row 2k", "andy_spirit_desc": "sit still",'
            ' "andy_account_desc": "invoice", "andy_relations_desc": "call"}'
        ),
    )

    token = csrf_token(auth_client, "/daily")
    response = auth_client.post(
        "/daily/generate-andy",
        data={"date": "2026-08-30", "job_nonce": "e" * 32, "_csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303

    settled = _await_terminal("andy_generation", seconds=10)
    assert settled == "succeeded", f"the woken pass never finished the job ({settled})"

    db = sqlite3.connect(user_db_path())
    try:
        stored = db.execute("SELECT andy_body_desc FROM daily_logs WHERE date = '2026-08-30'").fetchone()
    finally:
        db.close()
    assert stored == ("row 2k",)


def test_a_wake_is_bounded_and_never_runs_without_a_loop(monkeypatch):
    from app.services import job_worker

    monkeypatch.setattr("app.config.WORKER_WAKE", True)
    # No running loop: the caller is not in a request, so there is nothing to
    # attach a task to and the tick must remain the only path.
    assert job_worker.wake_worker("user", "user.db") is False

    monkeypatch.setattr("app.config.WORKER_WAKE", False)
    assert job_worker.wake_worker("user", "user.db") is False
    assert job_worker.WAKE_TASKS_MAX >= 1
    assert job_worker.WAKE_JOBS_MAX >= 1


def _answer(raw: str):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return raw

    return fake_call_llm


async def _always_available(_db) -> bool:
    return True


def _await_terminal(kind: str, *, seconds: int) -> str | None:
    """Poll the row the way a browser polls the tray, with a hard deadline."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        db = sqlite3.connect(user_db_path())
        try:
            row = db.execute("SELECT status FROM jobs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        finally:
            db.close()
        if row and row[0] in {"succeeded", "failed", "cancelled", "needs_attention"}:
            return row[0]
        time.sleep(0.1)
    return row[0] if row else None
