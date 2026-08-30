"""generate-andy must surface LLM failures, not swallow them; and the JSON parser
must tolerate the real prod failure mode (model wraps JSON in prose/fences).

Regression: the handler caught every exception and redirected to empty fields, so a
failing/misconfigured LLM looked like "request fires, nothing fills". Prod log showed
the real cause was a JSONDecodeError in parse_andy_response, not an auth error.
"""

import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import csrf_token, drain_jobs, user_db_path

from app.services.llm import parse_andy_response


async def _available(_db) -> bool:
    return True


@pytest.fixture(autouse=True)
def _clean_jobs(auth_client):
    """drain_jobs runs whatever is queued, so no test may inherit another's work."""
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute("DELETE FROM jobs")
        conn.execute("DELETE FROM llm_publications")
        conn.commit()
    finally:
        conn.close()
    yield


def test_parse_plain_json():
    assert parse_andy_response('{"andy_body_desc": "x"}') == {"andy_body_desc": "x"}


def test_parse_fenced_json():
    assert parse_andy_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_prose_wrapped_json():
    # the real prod failure mode: model adds prose/reasoning around the object
    assert parse_andy_response('Sure, here you go:\n{"a": 1, "b": 2}\nHope that helps!') == {"a": 1, "b": 2}


def test_parse_doubled_closing_brace():
    # exact prod failure: model emitted a valid object then a spurious extra '}'
    assert parse_andy_response('{"andy_body_desc": "x"}\n}\n') == {"andy_body_desc": "x"}


@pytest.mark.parametrize("bad", ["", "   \n  ", "no json here at all"])
def test_parse_rejects_non_json(bad):
    with pytest.raises(ValueError):
        parse_andy_response(bad)


def test_generate_andy_refuses_without_a_provider_instead_of_queueing(auth_client):
    """With no provider there is nothing to buy, so no job should exist to retry."""
    token = csrf_token(auth_client, "/daily")
    resp = auth_client.post(
        "/daily/generate-andy",
        data={"date": "2026-07-07", "job_nonce": "a" * 32, "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "err=" in resp.headers["location"]
    conn = sqlite3.connect(user_db_path())
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'andy_generation'").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_failed_generation_shows_its_reason_on_the_job_card(auth_client, monkeypatch):
    """The failure has to stay visible: a silent one looks exactly like
    "request fires, nothing fills", which is the regression this file exists for."""

    async def broken(db, system_prompt, user_prompt, **kwargs):
        raise ValueError("LLM authentication failed for model test/model")

    monkeypatch.setattr("app.routers.daily.llm_available", _available)
    monkeypatch.setattr("app.services.andy.call_llm", broken)

    token = csrf_token(auth_client, "/daily")
    resp = auth_client.post(
        "/daily/generate-andy",
        data={"date": "2026-07-07", "job_nonce": "b" * 32, "_csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    job_id = int(parse_qs(urlsplit(resp.headers["location"]).query)["job_id"][0])
    drain_jobs()

    card = auth_client.get(f"/api/jobs/{job_id}")
    assert "Failed" in card.text
    assert "LLM authentication failed" in card.text, "the reason must reach the user, not only the log"
    assert "Retry" in card.text


def test_generate_andy_sends_the_schedule_not_a_prescription(auth_client, monkeypatch):
    """The planner prompt carries the weekly schedule, never per-exercise targets.

    It used to render every non-archived, non-ad_hoc row of training_exercises
    as "- <name>: <sets>x<reps>". Two problems compounded: ad_hoc rows (created
    by the WOD parser, which never sets target_sets/target_reps) rendered as
    "- Thruster: NonexNone", and the rows that DID have targets described a
    basement kettlebell program the user had already left. The rows cannot
    simply be deleted — training_entries references them — so the fix was to
    stop reading them here at all.

    Both halves are asserted: the seeded prescription is absent, and the
    schedule that replaced it is present. Checking only the absence would pass
    against an empty prompt.
    """
    conn = sqlite3.connect(user_db_path())
    try:
        conn.execute(
            "INSERT INTO training_exercises (name, section, target_sets, target_reps, ad_hoc, archived) "
            "VALUES (?, 'Core', NULL, NULL, 1, 0)",
            ("ZZTestAdHocPromptMovement",),
        )
        conn.execute(
            "INSERT INTO training_exercises (name, section, target_sets, target_reps, ad_hoc, archived) "
            "VALUES (?, 'Core', 3, '10', 0, 1)",
            ("ZZTestArchivedPromptMovement",),
        )
        # The schedule has no default any more: an unset one reports "no fixed
        # training days set", which is the honest thing for a new user and the
        # wrong fixture for this test. Configure it explicitly.
        conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES ('training_days', 'mon,wed,fri')")
        conn.commit()
    finally:
        conn.close()

    captured: dict = {}

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        captured["user_prompt"] = user_prompt
        return '{"andy_body_desc": "x", "andy_spirit_desc": "x", "andy_account_desc": "x", "andy_relations_desc": "x"}'

    # The suggestions are built in the worker now, so the stub belongs where
    # that module bound the name.
    monkeypatch.setattr("app.services.andy.call_llm", fake_call_llm)

    monkeypatch.setattr("app.routers.daily.llm_available", _available)
    token = csrf_token(auth_client, "/daily")
    try:
        resp = auth_client.post(
            "/daily/generate-andy",
            data={"date": "2026-07-08", "job_nonce": "c" * 32, "_csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "user_prompt" not in captured, "the request must not wait on the provider"
        drain_jobs()
        assert "user_prompt" in captured, "the worker must have invoked call_llm"
        prompt = captured["user_prompt"]

        assert "--- Training plan ---" in prompt, "the schedule block must reach the planner"
        assert "Training days:" in prompt, "the schedule must name the configured days"
        # 2026-07-08 is a Wednesday. Without this, passing schedule_block the
        # wrong date — or a date offset by days — changed nothing observable.
        assert "Today is Wednesday" in prompt, "the block must describe the date actually being planned"

        assert "--- Training Protocol ---" not in prompt, "the prescription block is gone"
        assert "ZZTestAdHocPromptMovement" not in prompt
        assert "ZZTestArchivedPromptMovement" not in prompt
        assert "Jump Rope" not in prompt, (
            "seeded protocol rows must no longer be prescribed to the planner — "
            "they outlive the program that created them"
        )
    finally:
        conn = sqlite3.connect(user_db_path())
        try:
            conn.execute("DELETE FROM training_exercises WHERE name = 'ZZTestAdHocPromptMovement'")
            conn.execute("DELETE FROM training_exercises WHERE name = 'ZZTestArchivedPromptMovement'")
            conn.commit()
        finally:
            conn.close()
