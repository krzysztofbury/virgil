"""Drop the No Porn tables that nothing reads since Freedom (migration 034).

feniks_config: its start date, target and why moved onto the Porn habit in 034.
feniks_milestones: a fixed 90-day list from the original program with a done flag
per day; no page, API or export has shown it since the single-flow rework.

feniks_journal and feniks_pleasures stay: they hold the person's own writing and
/api/noporn, the markdown export and the importer still read them.
"""

import aiosqlite

_DROPPED = ("feniks_config", "feniks_milestones")


async def up(db: aiosqlite.Connection) -> None:
    for table in _DROPPED:
        await db.execute(f"DROP TABLE IF EXISTS {table}")
