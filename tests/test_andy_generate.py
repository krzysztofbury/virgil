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


def test_generate_andy_excludes_ad_hoc_and_archived_from_training_protocol(auth_client, monkeypatch):
    """The '--- Training Protocol ---' prompt block must only include real,
    active protocol movements. resolve_movement() never sets target_sets or
    target_reps on an ad_hoc row, so an unfiltered query renders it as
    "- Thruster: NonexNone" — after a month of CrossFit logging that garbage
    would dominate what the daily-planner LLM reads to plan the user's day.
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
        assert "--- Training Protocol ---" in captured["user_prompt"]
        assert "ZZTestAdHocPromptMovement" not in captured["user_prompt"], (
            "ad-hoc movements must not flood the daily-planner prompt"
        )
        assert "ZZTestArchivedPromptMovement" not in captured["user_prompt"], (
            "archived movements must not appear in the daily-planner prompt"
        )
        assert "Jump Rope" in captured["user_prompt"], "real, active protocol movements must still appear"
    finally:
        conn = sqlite3.connect(user_db_path())
        try:
            conn.execute("DELETE FROM training_exercises WHERE name = 'ZZTestAdHocPromptMovement'")
            conn.execute("DELETE FROM training_exercises WHERE name = 'ZZTestArchivedPromptMovement'")
            conn.commit()
        finally:
            conn.close()
