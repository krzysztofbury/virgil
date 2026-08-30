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
from tests.conftest import user_db_path


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM training_sessions WHERE notes LIKE 'SCHED %'")
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
