from app.i18n import N_

# Shared life area constants used by dashboard, life_scores, and export
LIFE_AREAS = ["planning", "spirituality", "health", "work", "social", "growth", "relaxation", "family"]
LIFE_AREA_LABELS = [
    N_("Planning"),
    N_("Spirituality"),
    N_("Health"),
    N_("Work"),
    N_("Social"),
    N_("Growth"),
    N_("Relaxation"),
    N_("Family"),
]


async def get_setting(db, key: str, default: str = "") -> str:
    assert key, "Setting key must not be empty"
    row = await db.execute_fetchall("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row[0]["value"] if row else default


async def set_setting(db, key: str, value: str) -> None:
    assert key, "Setting key must not be empty"
    assert isinstance(value, str), f"Setting value must be str, got {type(value).__name__}"
    await db.execute(
        "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (key, value),
    )
    await db.commit()


async def get_feature_flags(db) -> dict[str, bool]:
    """Load all feature_* settings as a {name: bool} dict."""
    rows = await db.execute_fetchall("SELECT key, value FROM app_settings WHERE key LIKE 'feature_%'")
    return {row["key"].removeprefix("feature_"): row["value"] == "1" for row in rows}
