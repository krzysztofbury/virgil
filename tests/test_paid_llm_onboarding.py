"""Onboarding buys nothing inside a request, and retries only what is missing."""

import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiosqlite
import pytest

import app.services.onboarding as onboarding_service
from app.services.job_handlers import handle_medical_import, handle_onboarding_enrichment
from app.services.job_worker import AmbiguousJobError, JobContext, VisibleJobError
from app.services.llm import LLMCallAmbiguousError
from app.services.llm_jobs import enqueue_paid_llm_job, llm_result_published, paid_llm_job_key
from tests.conftest import csrf_token, user_db_path

_ENRICHMENT = "onboarding_enrichment"
_MEDICAL = "medical_import"


def _reset() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("DELETE FROM jobs")
        db.execute("DELETE FROM llm_publications")
        db.execute("DELETE FROM blood_results")
        db.execute("DELETE FROM blood_markers WHERE category = 'Imported'")
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def clean(auth_client):
    _reset()
    yield
    _reset()


def _settle_jobs() -> None:
    db = sqlite3.connect(user_db_path())
    try:
        db.execute(
            "UPDATE jobs SET status = 'failed', attempts = 1, started_at = datetime('now'), "
            "finished_at = datetime('now') WHERE status = 'queued'"
        )
        db.commit()
    finally:
        db.close()


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


def _seed_profile(**fields) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    db = sqlite3.connect(user_db_path())
    try:
        db.execute("INSERT OR IGNORE INTO user_profiles (id) VALUES (1)")
        if fields:
            db.execute(f"UPDATE user_profiles SET {columns} WHERE id = 1", tuple(fields.values()))
        db.commit()
    finally:
        db.close()


def test_confirming_onboarding_never_waits_on_a_provider():
    source = Path("app/routers/onboarding.py").read_text()
    assert "run_enrichment" not in source
    assert "litellm" not in source, "the medical calls moved to app/services/medical_import.py"
    assert "acompletion" not in source


def _step_of(system_prompt: str) -> str:
    """Name the enrichment step from its system prompt, so a test can count
    purchases per step instead of depending on what else the shared database
    happens to hold."""
    if "creating a realistic daily schedule" in system_prompt:
        return "realistic_day"
    if system_prompt.startswith("You are a personal development"):
        return "profile_summary"
    if system_prompt.startswith("You are a goal-setting"):
        return "goal_expansion"
    return "habit_experiment"


def test_a_partial_failure_keeps_what_it_bought_and_retries_only_the_rest(monkeypatch):
    _seed_profile(sex="m", age=40, ideal_day="wake, train, work", habits_break="")
    calls: list[str] = []

    async def flaky(system_prompt, user_prompt, max_tokens=2048):
        step = _step_of(system_prompt)
        calls.append(step)
        if step == "profile_summary":
            return "a steady forty year old"
        if step == "realistic_day":
            raise ValueError("LLM rate limit exceeded for model test/model")
        return "[]"

    monkeypatch.setattr(onboarding_service, "_llm_call", flaky)

    async def run(key_part, expect_failure):
        db = await _db()
        try:
            key = paid_llm_job_key(_ENRICHMENT, key_part)
            queued = await enqueue_paid_llm_job(db, _ENRICHMENT, {}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            if expect_failure:
                with pytest.raises(VisibleJobError):
                    await handle_onboarding_enrichment(context, {})
                return None
            return await handle_onboarding_enrichment(context, {})
        finally:
            await db.close()

    asyncio.run(run("confirm", True))
    assert calls.count("profile_summary") == 1
    assert calls.count("realistic_day") == 1, "a failed step must not stop the ones after it"
    assert _rows("SELECT llm_summary, realistic_day FROM user_profiles")[0] == {
        "llm_summary": "a steady forty year old",
        "realistic_day": None,
    }

    # The worker would have moved the failed job out of the queue; do the same,
    # because one paid kind may hold a single queued job at a time.
    _settle_jobs()
    calls.clear()

    async def working(system_prompt, user_prompt, max_tokens=2048):
        calls.append(_step_of(system_prompt))
        return "a realistic day" if _step_of(system_prompt) == "realistic_day" else "[]"

    monkeypatch.setattr(onboarding_service, "_llm_call", working)
    result = asyncio.run(run("retry", False))

    assert calls.count("profile_summary") == 0, "a published step must never be bought again"
    assert calls.count("realistic_day") == 1
    assert result["steps"]["profile_summary"] == "already_published"
    assert result["steps"]["realistic_day"] == "published"
    assert _rows("SELECT realistic_day FROM user_profiles")[0]["realistic_day"] == "a realistic day"


def test_an_uncertain_step_asks_for_review(monkeypatch):
    _seed_profile(sex="m", age=40, ideal_day="", habits_break="")

    async def uncertain(system_prompt, user_prompt, max_tokens=2048):
        raise LLMCallAmbiguousError("timed out")

    monkeypatch.setattr(onboarding_service, "_llm_call", uncertain)

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_ENRICHMENT, "confirm")
            queued = await enqueue_paid_llm_job(db, _ENRICHMENT, {}, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(AmbiguousJobError):
                await handle_onboarding_enrichment(context, {})
            return await llm_result_published(
                db, _ENRICHMENT, onboarding_service.enrichment_step_key("profile_summary")
            )
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False


def test_a_step_with_no_input_is_reported_as_not_needed(auth_client):
    _seed_profile(sex="", age=None, family="", ideal_day="", habits_break="")

    async def scenario():
        db = await _db()
        try:
            return await onboarding_service.enrichment_progress(db)
        finally:
            await db.close()

    progress = {item["step"]: item["state"] for item in asyncio.run(scenario())}
    assert progress["profile_summary"] == "not_needed"
    assert progress["realistic_day"] == "not_needed"
    assert progress["habit_experiment"] == "not_needed"


def test_the_feniks_trigger_is_a_word_match_not_a_purchase():
    assert onboarding_service.apply_feniks_trigger_words({"habits_break": "porno every night"}) is True
    assert onboarding_service.apply_feniks_trigger_words({"habits_bad": "nofap"}) is True
    assert onboarding_service.apply_feniks_trigger_words({"habits_break": "biting nails"}) is False


def test_a_medical_upload_is_staged_and_never_put_in_the_payload(auth_client, monkeypatch, tmp_path):
    from app.services import medical_import

    monkeypatch.setattr(medical_import, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr("app.config.INTERNAL_LLM_KEY", "test-key")

    token = csrf_token(auth_client, "/onboarding?step=5")
    response = auth_client.post(
        "/onboarding/step/5",
        data={"medical_text": "HGB: 15.8 g/dl", "_csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "err=" not in location, location
    job_id = int(parse_qs(urlsplit(location).query)["job_id"][0])

    payload = _rows("SELECT payload_json FROM jobs WHERE id = ?", (job_id,))[0]["payload_json"]
    assert "HGB" not in payload, "medical text must never reach the jobs table"
    assert json.loads(payload)["source"] == "text"
    staged = list((tmp_path / "staging").rglob("*.bin"))
    assert len(staged) == 1
    assert staged[0].read_bytes() == b"HGB: 15.8 g/dl"


def test_the_import_stores_markers_then_deletes_the_staged_bytes(monkeypatch, tmp_path):
    from app.services import medical_import

    monkeypatch.setattr(medical_import, "STAGING_DIR", tmp_path / "staging")

    async def fake_parse(text):
        assert "HGB" in text
        return [{"marker": "HGB", "unit": "g/dl", "results": [{"date": "2026-08-01", "value": 15.8, "flag": ""}]}]

    monkeypatch.setattr(medical_import, "parse_medical_text", fake_parse)
    upload = medical_import.stage_upload("user", b"HGB: 15.8 g/dl")
    payload = {"source": "text", "upload": upload}

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_MEDICAL, upload)
            queued = await enqueue_paid_llm_job(db, _MEDICAL, payload, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            return await handle_medical_import(context, payload)
        finally:
            await db.close()

    result = asyncio.run(scenario())
    assert result == {"published": True, "markers": 1}
    assert _rows("SELECT name FROM blood_markers WHERE name = 'HGB'")
    assert not list((tmp_path / "staging").rglob("*.bin")), "the bytes exist only to be imported"


def test_a_failed_import_keeps_the_bytes_for_an_explicit_retry(monkeypatch, tmp_path):
    from app.services import medical_import

    monkeypatch.setattr(medical_import, "STAGING_DIR", tmp_path / "staging")

    async def nothing(text):
        return []

    monkeypatch.setattr(medical_import, "parse_medical_text", nothing)
    upload = medical_import.stage_upload("user", b"unreadable")
    payload = {"source": "text", "upload": upload}

    async def scenario():
        db = await _db()
        try:
            key = paid_llm_job_key(_MEDICAL, upload)
            queued = await enqueue_paid_llm_job(db, _MEDICAL, payload, idempotency_key=key)
            context = JobContext(db=db, user_id="user", job_id=queued.job_id, attempt=1)
            with pytest.raises(VisibleJobError, match="No blood test markers"):
                await handle_medical_import(context, payload)
            return await llm_result_published(db, _MEDICAL, key)
        finally:
            await db.close()

    assert asyncio.run(scenario()) is False
    assert list((tmp_path / "staging").rglob("*.bin")), "a retry needs the upload it never imported"


def test_a_staged_token_cannot_reach_another_users_directory(tmp_path, monkeypatch):
    from app.services import medical_import

    monkeypatch.setattr(medical_import, "STAGING_DIR", tmp_path / "staging")
    token = medical_import.stage_upload("alice", b"private")

    assert medical_import.read_staged("alice", token) == b"private"
    with pytest.raises(ValueError):
        medical_import.read_staged("bob", token)
    for bad in ("../../etc/passwd", "", "not-hex", "a" * 33):
        with pytest.raises(ValueError):
            medical_import.staged_path("alice", bad)


def test_staged_uploads_do_not_linger(tmp_path, monkeypatch):
    import os
    import time

    from app.services import medical_import

    monkeypatch.setattr(medical_import, "STAGING_DIR", tmp_path / "staging")
    token = medical_import.stage_upload("alice", b"old")
    path = medical_import.staged_path("alice", token)
    stale = time.time() - medical_import.STAGED_MAX_AGE_SECONDS - 60
    os.utime(path, (stale, stale))

    assert medical_import.prune_staged("alice") == 1
    assert not path.exists()
