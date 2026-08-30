"""Bound each workload kind to one queued job behind the current runner."""

import json
from datetime import UTC, datetime

INDEX_NAME = "idx_jobs_queued_workload_kind"
INDEX_SQL = (
    f"CREATE UNIQUE INDEX {INDEX_NAME} ON jobs(kind) "
    "WHERE status = 'queued' AND kind IN ('backup', 'markdown_export', 'oura_sync')"
)


def _normalized(sql: str) -> str:
    return "".join(sql.lower().split()).replace("ifnotexists", "")


def _oura_range(payload_json: str) -> int:
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError):
        return -1
    days_back = payload.get("days_back") if isinstance(payload, dict) else None
    return days_back if isinstance(days_back, int) and not isinstance(days_back, bool) else -1


async def _reconcile_queued_duplicates(db) -> None:
    groups = await db.execute_fetchall(
        """SELECT kind FROM jobs
           WHERE status = 'queued' AND kind IN ('backup', 'markdown_export', 'oura_sync')
           GROUP BY kind HAVING count(*) > 1"""
    )
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
    for group in groups:
        kind = group["kind"]
        rows = await db.execute_fetchall(
            "SELECT id, payload_json FROM jobs WHERE kind = ? AND status = 'queued' ORDER BY id",
            (kind,),
        )
        survivor = (
            max(rows, key=lambda row: (_oura_range(row["payload_json"]), -row["id"]))
            if kind == "oura_sync"
            else rows[0]
        )
        for row in rows:
            if row["id"] == survivor["id"]:
                continue
            await db.execute(
                """UPDATE jobs
                   SET status = 'cancelled', last_error = 'Superseded while bounding the workload queue.',
                       finished_at = ?, updated_at = ?
                   WHERE id = ? AND status = 'queued'""",
                (now, now, row["id"]),
            )


async def up(db) -> None:
    await _reconcile_queued_duplicates(db)
    await db.execute(INDEX_SQL.replace("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX IF NOT EXISTS", 1))
    rows = await db.execute_fetchall("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?", (INDEX_NAME,))
    if len(rows) != 1 or _normalized(rows[0]["sql"] or "") != _normalized(INDEX_SQL):
        raise RuntimeError(f"Index {INDEX_NAME} has an unsupported definition")
