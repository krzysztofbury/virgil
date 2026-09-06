"""Index bounded training history summaries and selected-day details."""

import aiosqlite

_INDEXES = {
    "idx_training_sessions_date_id": (
        "CREATE INDEX IF NOT EXISTS idx_training_sessions_date_id ON training_sessions(date, id DESC)",
        "training_sessions",
        "CREATE INDEX idx_training_sessions_date_id ON training_sessions(date, id DESC)",
    ),
    "idx_training_entries_session_id": (
        "CREATE INDEX IF NOT EXISTS idx_training_entries_session_id ON training_entries(session_id)",
        "training_entries",
        "CREATE INDEX idx_training_entries_session_id ON training_entries(session_id)",
    ),
}


def _normalise_sql(sql: str) -> str:
    return " ".join(sql.casefold().split())


async def up(db: aiosqlite.Connection) -> None:
    for name, (sql, expected_table, expected_sql) in _INDEXES.items():
        await db.execute(sql)
        rows = await db.execute_fetchall(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        )
        actual = tuple(rows[0]) if rows else None
        expected = (expected_table, _normalise_sql(expected_sql))
        normalised_actual = (actual[0], _normalise_sql(actual[1])) if actual and actual[1] else actual
        if normalised_actual != expected:
            raise RuntimeError(f"Index {name} has incompatible definition: {actual!r}")
