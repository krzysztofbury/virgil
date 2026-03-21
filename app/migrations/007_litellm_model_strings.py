"""Convert existing llm_providers rows to LiteLLM model string format.

Old format: provider='claude', model='claude-sonnet-4-20250514'
New format: provider='anthropic', model='anthropic/claude-sonnet-4-20250514'
"""

PROVIDER_MAP = {
    "claude": ("anthropic", "anthropic/"),
    "openai": ("openai", "openai/"),
    "gemini": ("gemini", "gemini/"),
}


async def up(db):
    rows = await db.execute_fetchall("SELECT id, provider, model FROM llm_providers")
    for row in rows:
        old_provider = row["provider"]
        old_model = row["model"]
        if old_provider in PROVIDER_MAP:
            new_provider, prefix = PROVIDER_MAP[old_provider]
            # Only prefix if not already in LiteLLM format.
            new_model = f"{prefix}{old_model}" if not old_model.startswith(prefix) else old_model
            await db.execute(
                "UPDATE llm_providers SET provider = ?, model = ? WHERE id = ?",
                (new_provider, new_model, row["id"]),
            )
