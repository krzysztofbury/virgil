"""Add the per-user durable job queue.

Every DDL statement is independently retry-safe because the per-user migration
runner cannot make DDL and its version marker one atomic transaction.
"""

import aiosqlite

_CREATE_JOBS_SQL = """CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL
        CHECK(length(kind) BETWEEN 1 AND 64 AND kind = trim(kind)),
    payload_json TEXT NOT NULL DEFAULT '{}'
        CHECK(length(payload_json) <= 16384),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'needs_attention')),
    idempotency_key TEXT
        CHECK(idempotency_key IS NULL OR (
            length(idempotency_key) BETWEEN 1 AND 200
            AND idempotency_key = trim(idempotency_key)
        )),
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK(attempts BETWEEN 0 AND 100),
    max_attempts INTEGER NOT NULL DEFAULT 3
        CHECK(max_attempts BETWEEN 1 AND 100 AND attempts <= max_attempts),
    retry_policy TEXT NOT NULL DEFAULT 'manual'
        CHECK(retry_policy IN ('automatic', 'manual')),
    run_after TEXT NOT NULL DEFAULT (datetime('now')),
    locked_at TEXT,
    locked_by TEXT CHECK(locked_by IS NULL OR length(locked_by) BETWEEN 1 AND 100),
    claim_token TEXT CHECK(claim_token IS NULL OR (
        length(claim_token) = 32 AND claim_token NOT GLOB '*[^0-9a-f]*'
    )),
    last_error TEXT NOT NULL DEFAULT '' CHECK(length(last_error) <= 500),
    result_json TEXT NOT NULL DEFAULT '{}' CHECK(length(result_json) <= 16384),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK((status = 'running') = (locked_at IS NOT NULL)),
    CHECK((status = 'running') = (locked_by IS NOT NULL)),
    CHECK((status = 'running') = (claim_token IS NOT NULL)),
    CHECK((status IN ('succeeded', 'failed', 'cancelled', 'needs_attention')) = (finished_at IS NOT NULL)),
    CHECK(status != 'queued' OR attempts < max_attempts),
    CHECK(status != 'running' OR attempts BETWEEN 1 AND max_attempts),
    CHECK(status NOT IN ('succeeded', 'failed', 'needs_attention') OR attempts BETWEEN 1 AND max_attempts),
    CHECK(status NOT IN ('running', 'succeeded', 'failed', 'needs_attention') OR started_at IS NOT NULL)
)"""

_INDEX_SQL = {
    "idx_jobs_idempotency": (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
        "ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
    ),
    "idx_jobs_claimable": (
        "CREATE INDEX IF NOT EXISTS idx_jobs_claimable ON jobs(run_after, id) WHERE status = 'queued'"
    ),
    "idx_jobs_stale": "CREATE INDEX IF NOT EXISTS idx_jobs_stale ON jobs(locked_at, id) WHERE status = 'running'",
    "idx_jobs_recent": "CREATE INDEX IF NOT EXISTS idx_jobs_recent ON jobs(created_at DESC, id DESC)",
}

_REQUIRED_COLUMNS = {
    "id",
    "kind",
    "payload_json",
    "status",
    "idempotency_key",
    "attempts",
    "max_attempts",
    "retry_policy",
    "run_after",
    "locked_at",
    "locked_by",
    "claim_token",
    "last_error",
    "result_json",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
}


def _normalized_schema_sql(sql: str) -> str:
    return " ".join(sql.split()).replace(" IF NOT EXISTS", "")


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(_CREATE_JOBS_SQL)

    columns = {column[1] for column in await db.execute_fetchall("PRAGMA table_info(jobs)")}
    missing = _REQUIRED_COLUMNS - columns
    if missing:
        raise RuntimeError(f"existing jobs table has an incompatible schema: missing={sorted(missing)}")

    table_rows = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'jobs'")
    if not table_rows or _normalized_schema_sql(table_rows[0][0]) != _normalized_schema_sql(_CREATE_JOBS_SQL):
        raise RuntimeError("existing jobs table has an incompatible definition")

    for statement in _INDEX_SQL.values():
        await db.execute(statement)

    index_rows = await db.execute_fetchall(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name IN (?, ?, ?, ?)",
        tuple(_INDEX_SQL),
    )
    actual_indexes = {row[0]: _normalized_schema_sql(row[1]) for row in index_rows}
    expected_indexes = {name: _normalized_schema_sql(sql) for name, sql in _INDEX_SQL.items()}
    if actual_indexes != expected_indexes:
        raise RuntimeError("existing jobs indexes have incompatible definitions")
