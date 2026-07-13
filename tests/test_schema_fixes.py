"""Schema regressions: llm_providers CHECK removal (012) and exercise archive (013)."""

import sqlite3

from conftest import user_db_path


def test_llm_provider_accepts_litellm_names(auth_client):
    """Migration 007 renames claude→anthropic and the UI offers mistral/groq/
    ollama — the original CHECK(provider IN ('claude','openai','gemini'))
    rejected all of them."""
    conn = sqlite3.connect(user_db_path())
    try:
        for provider in ("anthropic", "mistral", "groq", "ollama", "other"):
            conn.execute(
                "INSERT INTO llm_providers (provider, api_key_enc, model, is_active) VALUES (?, 'enc', 'm', 0)",
                (provider,),
            )
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM llm_providers").fetchone()[0]
        assert n >= 5
        conn.execute("DELETE FROM llm_providers")
        conn.commit()
    finally:
        conn.close()


def test_training_exercises_have_archived_column(auth_client):
    conn = sqlite3.connect(user_db_path())
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(training_exercises)").fetchall()]
        assert "archived" in cols
        archived = conn.execute("SELECT COUNT(*) FROM training_exercises WHERE archived = 1").fetchone()[0]
        assert archived == 0, "Seeded exercises must start unarchived"
    finally:
        conn.close()
