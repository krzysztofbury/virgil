"""The server's own AI keys (#179), kept like an account's llm_providers.

The active one is what an account allowed to spend the server's key uses when
it has no key of its own. With none active, VIRGIL_INTERNAL_LLM_KEY and
VIRGIL_INTERNAL_LLM_MODEL from the environment still apply.
"""

import logging

from app.central_db import get_central_db
from app.services.encryption import decrypt, encrypt

logger = logging.getLogger(__name__)


def _masked(plain: str) -> str:
    return f"...{plain[-4:]}" if len(plain) > 8 else "****"


async def list_providers() -> list[dict]:
    db = await get_central_db()
    rows = await db.execute_fetchall(
        "SELECT id, provider, model, is_active, created_at, api_key_enc FROM server_llm_providers ORDER BY id DESC"
    )
    providers = []
    for row in rows:
        provider = dict(row)
        try:
            provider["api_key_masked"] = _masked(decrypt(provider.pop("api_key_enc")))
        except Exception:
            provider["api_key_masked"] = "(encrypted)"
        providers.append(provider)
    return providers


async def add_provider(provider: str, model: str, api_key: str) -> int:
    db = await get_central_db()
    cursor = await db.execute(
        "INSERT INTO server_llm_providers (provider, api_key_enc, model, is_active) VALUES (?, ?, ?, 0)",
        (provider, encrypt(api_key), model),
    )
    await db.commit()
    return cursor.lastrowid


async def activate_provider(provider_id: int) -> bool:
    db = await get_central_db()
    if not await db.execute_fetchall("SELECT 1 FROM server_llm_providers WHERE id = ?", (provider_id,)):
        return False
    await db.execute("UPDATE server_llm_providers SET is_active = (id = ?)", (provider_id,))
    await db.commit()
    return True


async def delete_provider(provider_id: int) -> bool:
    db = await get_central_db()
    cursor = await db.execute("DELETE FROM server_llm_providers WHERE id = ?", (provider_id,))
    await db.commit()
    return cursor.rowcount == 1


async def server_model_and_key() -> tuple[str, str] | None:
    """The server's (model, key): the active saved key, else the environment's, else None."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT model, api_key_enc FROM server_llm_providers WHERE is_active = 1")
    if rows:
        try:
            key = decrypt(rows[0]["api_key_enc"])
        except Exception:
            # Saved under another encryption key: no server key, not an error page.
            logger.error("The server AI key in use cannot be decrypted; add it again in Settings > Server")
            return None
        if key and rows[0]["model"]:
            return rows[0]["model"], key
    from app import config

    if config.INTERNAL_LLM_KEY and config.INTERNAL_LLM_MODEL:
        return config.INTERNAL_LLM_MODEL, config.INTERNAL_LLM_KEY
    return None


async def active_model() -> str:
    """The model the server's key would use, without decrypting the key, or ""."""
    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT model FROM server_llm_providers WHERE is_active = 1")
    if rows:
        return rows[0]["model"]
    from app import config

    return config.INTERNAL_LLM_MODEL if config.INTERNAL_LLM_KEY else ""
