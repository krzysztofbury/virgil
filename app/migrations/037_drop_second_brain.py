"""Remove what the Second Brain folder left behind.

Virgil no longer reads markdown from a server-side folder or writes exports into
it: markdown export is a download now. The scheduled export settings, the goal
import marker and any export jobs still in the queue have no code left to read or
run them; a queued export job would otherwise fail as an unsupported kind and
show up as an error.
"""

import aiosqlite

_SETTINGS = ("export_enabled", "export_interval_hours", "export_last_run", "cele_bootstrap_version")


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("DELETE FROM jobs WHERE kind = 'markdown_export'")
    await db.execute(
        f"DELETE FROM app_settings WHERE key IN ({', '.join('?' for _ in _SETTINGS)})",  # noqa: S608 - placeholders only
        _SETTINGS,
    )
