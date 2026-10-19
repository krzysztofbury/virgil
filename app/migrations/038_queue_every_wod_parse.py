"""Let every captured training note queue its own parse.

Migration 029 allowed one queued job per paid kind. For a WOD parse that cap
dropped work: a note captured while another note's parse was still queued was
saved with no parse queued at all, and nothing ever read it. Each parse is keyed
by its session, so a double submit still queues one, and the capture route's
rate limit bounds the spend. The other paid kinds keep the cap.
"""

import aiosqlite

# Frozen at the version this migration shipped. app.services.llm_jobs owns the
# live tuple; a test asserts the two match.
_KINDS = (
    "andy_generation",
    "experiment_summary",
    "medical_import",
    "morning_briefing",
    "onboarding_enrichment",
)
_INDEX_NAME = "idx_jobs_queued_paid_llm_kind"
_KINDS_SQL = ", ".join(f"'{kind}'" for kind in _KINDS)


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    await db.execute(
        f"CREATE UNIQUE INDEX {_INDEX_NAME} ON jobs(kind) WHERE status = 'queued' AND kind IN ({_KINDS_SQL})"
    )
