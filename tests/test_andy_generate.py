"""generate-andy must surface LLM failures, not swallow them; and the JSON parser
must tolerate the real prod failure mode (model wraps JSON in prose/fences).

Regression: the handler caught every exception and redirected to empty fields, so a
failing/misconfigured LLM looked like "request fires, nothing fills". Prod log showed
the real cause was a JSONDecodeError in parse_andy_response, not an auth error.
"""

import sqlite3

import pytest
from conftest import csrf_token, user_db_path

from app.services.llm import parse_andy_response


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


def test_generate_andy_surfaces_error(auth_client):
    token = csrf_token(auth_client, "/daily")
    resp = auth_client.post(
        "/daily/generate-andy",
        data={"date": "2026-07-07", "_csrf_token": token},
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#andy-error", "error must retarget to the visible container"
    assert resp.headers.get("HX-Reswap") == "innerHTML"
    # Exact reason varies by env (no provider / bad key / bad model), but an LLM
    # error must be shown to the user, not swallowed into an empty redirect.
    assert "LLM" in resp.text and "⚠" in resp.text, f"reason must be shown, got: {resp.text[:200]}"


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
        conn.commit()
    finally:
        conn.close()

    captured: dict = {}

    async def fake_call_llm(db, system_prompt, user_prompt, **kwargs):
        captured["user_prompt"] = user_prompt
        return '{"andy_body_desc": "x", "andy_spirit_desc": "x", "andy_account_desc": "x", "andy_relations_desc": "x"}'

    import app.services.llm as llm_module

    monkeypatch.setattr(llm_module, "call_llm", fake_call_llm)

    token = csrf_token(auth_client, "/daily")
    try:
        resp = auth_client.post(
            "/daily/generate-andy",
            data={"date": "2026-07-08", "_csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "user_prompt" in captured, "call_llm must have been invoked"
        prompt = captured["user_prompt"]

        assert "--- Training plan ---" in prompt, "the schedule block must reach the planner"
        assert "CrossFit days:" in prompt, "the schedule must name the configured days"

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
