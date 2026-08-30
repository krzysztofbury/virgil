"""WOD parsing is a durable job: one paid parse per captured note, ever."""

import asyncio
import json
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

import app.services.wod_parser as wod_parser
from app.services.job_handlers import handle_wod_parse
from app.services.job_worker import AmbiguousJobError, JobContext
from app.services.llm import LLMCallAmbiguousError
from app.services.llm_jobs import enqueue_paid_llm_job, llm_result_published, paid_llm_job_key
from app.services.wod_parser import ParsedEntry, ParsedWod
from tests.conftest import csrf_token, drain_jobs, user_db_path

_KIND = "wod_parse"


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute(
            "DELETE FROM training_entries WHERE session_id IN (SELECT id FROM training_sessions WHERE notes LIKE 'PJ %')"
        )
        db.execute("DELETE FROM training_sessions WHERE notes LIKE 'PJ %'")
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


def _capture(auth_client, text: str = "PJ 21-15-9 thruster 43") -> tuple[int, int]:
    token = csrf_token(auth_client, "/training")
    response = auth_client.post(
        "/training/wod",
        data={"date": "2026-08-11", "duration_minutes": "40", "wod_text": text, "_csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    session_id = int(target.path.rsplit("/", 1)[1])
    job_id = int(parse_qs(target.query)["job_id"][0])
    return session_id, job_id


def test_capture_returns_before_any_provider_is_called(auth_client, monkeypatch):
    called = []

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        called.append(user_prompt)
        return json.dumps({"entries": [], "unmatched": []})

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    session_id, job_id = _capture(auth_client)

    assert called == [], "the request must not wait on the provider"
    job = _rows("SELECT kind, status, max_attempts, retry_policy FROM jobs WHERE id = ?", (job_id,))[0]
    assert (job["kind"], job["status"], job["max_attempts"], job["retry_policy"]) == (_KIND, "queued", 1, "manual")
    assert _rows("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"] is None

    drain_jobs()
    assert len(called) == 1


def test_the_confirm_screen_waits_instead_of_bouncing_the_user_away(auth_client, monkeypatch):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return json.dumps({"entries": [], "unmatched": []})

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    session_id, job_id = _capture(auth_client)

    waiting = auth_client.get(f"/training/wod/confirm/{session_id}?job_id={job_id}")
    assert waiting.status_code == 200
    assert "Analizuję notatkę" in waiting.text
    assert 'hx-target="#wod-confirm-root"' in waiting.text
    assert f'data-job-id="{job_id}"' in waiting.text, "the job itself reports in the tray"

    training = auth_client.get("/training")
    assert "Analizuję notatkę" in training.text
    assert "Wpisz serie ręcznie" not in training.text, "a parse in flight is not a stranded session"

    drain_jobs()
    settled = auth_client.get(f"/training/wod/confirm/{session_id}")
    assert "Analizuję notatkę" not in settled.text
    assert 'name="session_id"' in settled.text


def test_a_session_with_no_job_and_no_parse_still_redirects_away(auth_client):
    db = sqlite3.connect(user_db_path())
    try:
        cursor = db.execute("INSERT INTO training_sessions (date, notes) VALUES ('2026-08-11', 'PJ orphan')")
        session_id = cursor.lastrowid
        db.commit()
    finally:
        db.close()

    response = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/training"


def test_a_deterministic_parse_failure_still_publishes_so_manual_entry_is_reachable(monkeypatch):
    async def broken(db, text):
        raise ValueError("the model returned prose")

    monkeypatch.setattr(wod_parser, "parse_wod", broken)

    session_id = _seed_session("PJ handler failure")

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, str(session_id))
            queued = await enqueue_paid_llm_job(db, _KIND, {"session_id": session_id}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            result = await handle_wod_parse(context, {"session_id": session_id})
            return result, await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    result, published = asyncio.run(scenario())
    assert result["published"] is True
    assert result["parsed"] is False
    assert published is True, "the provider was paid, so a retry must not pay again"
    stored = json.loads(_rows("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"])
    assert stored["parse_error"] == "the model returned prose"
    assert stored["entries"] == []


def test_an_uncertain_provider_outcome_publishes_nothing(monkeypatch):
    async def uncertain(db, text):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr(wod_parser, "parse_wod", uncertain)
    session_id = _seed_session("PJ ambiguous")

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, str(session_id))
            queued = await enqueue_paid_llm_job(db, _KIND, {"session_id": session_id}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_wod_parse(context, {"session_id": session_id})
            return await llm_result_published(db, _KIND, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert _rows("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))[0]["wod_parsed"] is None


def test_a_replayed_parse_does_not_buy_the_answer_twice(monkeypatch):
    calls = []

    async def counted(db, text):
        calls.append(text)
        entry = ParsedEntry(movement="Thruster", set_number=1, reps=21, weight=43.0, duration=None, note="")
        return ParsedWod(entries=[entry], unmatched=[], dropped=0)

    monkeypatch.setattr(wod_parser, "parse_wod", counted)
    session_id = _seed_session("PJ replay")

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_KIND, str(session_id))
            queued = await enqueue_paid_llm_job(db, _KIND, {"session_id": session_id}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            first = await handle_wod_parse(context, {"session_id": session_id})
            second = await handle_wod_parse(context, {"session_id": session_id})
            return first, second
        finally:
            await db.close()

    first, second = asyncio.run(scenario())
    assert first["published"] is True and first["entries"] == 1
    assert second == {"published": False, "reason": "already_published"}
    assert len(calls) == 1


def test_the_key_is_the_session_so_one_note_is_never_parsed_twice(auth_client, monkeypatch):
    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        return json.dumps({"entries": [], "unmatched": []})

    monkeypatch.setattr(wod_parser, "call_llm", fake_call_llm)
    session_id, job_id = _capture(auth_client)

    async def scenario():
        db = await _db()
        try:
            return await enqueue_paid_llm_job(
                db,
                _KIND,
                {"session_id": session_id},
                idempotency_key=paid_llm_job_key(_KIND, str(session_id)),
            )
        finally:
            await db.close()

    assert asyncio.run(scenario()) == type(asyncio.run(scenario()))(job_id=job_id, created=False)


@pytest.mark.parametrize(
    "payload", [{"session_id": 0}, {"session_id": "7"}, {}, {"session_id": 1, "trigger": "manual"}]
)
def test_a_stored_parse_payload_is_validated(payload, monkeypatch):
    async def must_not_run(db, text):
        raise AssertionError("no provider call for an invalid payload")

    monkeypatch.setattr(wod_parser, "parse_wod", must_not_run)

    async def scenario():
        db = await _db()
        try:
            context = JobContext(db=db, user_id="user", job_id=1, attempt=1)
            with pytest.raises(ValueError):
                await handle_wod_parse(context, payload)
        finally:
            await db.close()

    asyncio.run(scenario())


def _seed_session(note: str) -> int:
    db = sqlite3.connect(user_db_path())
    try:
        cursor = db.execute("INSERT INTO training_sessions (date, notes) VALUES ('2026-08-11', ?)", (note,))
        db.commit()
        return int(cursor.lastrowid)
    finally:
        db.close()


def test_the_note_must_still_exist_before_anything_is_bought(monkeypatch):
    async def must_not_run(db, text):
        raise AssertionError("no provider call for a vanished note")

    monkeypatch.setattr(wod_parser, "parse_wod", must_not_run)

    async def scenario():
        db = await _db()
        try:
            context = JobContext(db=db, user_id="user", job_id=1, attempt=1)
            with pytest.raises(ValueError, match="gone"):
                await handle_wod_parse(context, {"session_id": 999999})
        finally:
            await db.close()

    asyncio.run(scenario())


def test_no_request_path_parses_a_wod():
    from pathlib import Path

    source = Path("app/routers/training.py").read_text()
    assert not re.search(r"\bawait parse_wod\(", source), "the route must only enqueue the parse"


def test_a_failed_parse_takes_the_waiting_screen_somewhere_useful(auth_client, monkeypatch):
    """The waiting panel polls itself. Once the job is terminal the session has
    no parse and no job, so the GET redirects - and an HTMX poll that follows a
    redirect selects #wod-confirm-root out of a page that has none, which blanks
    the panel and leaves the user on an empty screen with no way forward."""

    async def uncertain(db, system_prompt, user_prompt, **kwargs):
        raise LLMCallAmbiguousError("timed out")

    # An uncertain outcome is the one that withholds the write, so the session
    # keeps a NULL wod_parsed while its job is already terminal.
    monkeypatch.setattr(wod_parser, "call_llm", uncertain)
    session_id, job_id = _capture(auth_client, "PJ parse that times out")
    drain_jobs()

    polled = auth_client.get(
        f"/training/wod/confirm/{session_id}",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )

    assert polled.headers.get("HX-Redirect") == "/training", (
        "an HTMX poll must be told to navigate, not handed a page it cannot select from"
    )
    plain = auth_client.get(f"/training/wod/confirm/{session_id}", follow_redirects=False)
    assert plain.status_code == 303, "a plain browser GET keeps its redirect"
