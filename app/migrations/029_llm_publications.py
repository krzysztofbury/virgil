"""Add replay protection and a bounded queue for paid LLM jobs."""

import aiosqlite

# Frozen at the version this migration shipped. app.services.llm_jobs owns the
# live tuple; a test asserts the two match, so growing it means a new migration.
_KINDS = (
    "andy_generation",
    "experiment_summary",
    "medical_import",
    "morning_briefing",
    "onboarding_enrichment",
    "wod_parse",
)

_CREATE_PUBLICATIONS_SQL = """CREATE TABLE IF NOT EXISTS llm_publications (
    idempotency_key TEXT PRIMARY KEY
        CHECK(length(idempotency_key) BETWEEN 1 AND 200 AND idempotency_key = trim(idempotency_key)),
    kind TEXT NOT NULL
        CHECK(length(kind) BETWEEN 1 AND 64 AND kind = trim(kind)),
    job_id INTEGER NOT NULL CHECK(job_id > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_INDEX_NAME = "idx_jobs_queued_paid_llm_kind"
_KINDS_SQL = ", ".join(f"'{kind}'" for kind in _KINDS)
_INDEX_SQL = f"CREATE UNIQUE INDEX {_INDEX_NAME} ON jobs(kind) WHERE status = 'queued' AND kind IN ({_KINDS_SQL})"

_REQUIRED_COLUMNS = {"idempotency_key", "kind", "job_id", "created_at"}


def _normalized(sql: str) -> str:
    return " ".join(sql.split()).replace(" IF NOT EXISTS", "")


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(_CREATE_PUBLICATIONS_SQL)

    columns = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(llm_publications)")}
    if columns != _REQUIRED_COLUMNS:
        raise RuntimeError("existing llm_publications table has an incompatible schema")

    table_rows = await db.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_publications'"
    )
    if len(table_rows) != 1 or _normalized(table_rows[0][0] or "") != _normalized(_CREATE_PUBLICATIONS_SQL):
        raise RuntimeError("existing llm_publications table has an incompatible definition")

    await db.execute(_INDEX_SQL.replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1))
    index_rows = await db.execute_fetchall(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (_INDEX_NAME,)
    )
    if len(index_rows) != 1 or _normalized(index_rows[0][0] or "") != _normalized(_INDEX_SQL):
        raise RuntimeError(f"Index {_INDEX_NAME} has an unsupported definition")
