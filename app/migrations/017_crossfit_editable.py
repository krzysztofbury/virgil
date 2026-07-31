"""Make the seeded CrossFit movements user-editable.

016 seeded them with builtin = 1, which app/library_validation.py's
validate_library_write treats as "protected": update and delete both refuse
anything but 'archived', so the row could only be archived. The CrossFit
vocabulary is the user's to curate, and it also drives the WOD parser's
closed prompt — a movement they cannot remove is a movement the parser will
keep proposing.

016 was fixed too, for fresh installs; this migration exists because 016 is
already recorded in schema_migrations on databases that ran it.

Migration 019 later drops `category` entirely — on a fresh install (009 and
016 already write the post-019 shape from the start, so `category` never
exists) this migration must be a no-op rather than raise "no such column",
hence the guard.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    cols = await db.execute_fetchall("PRAGMA table_info(exercise_library)")
    if any(c["name"] == "category" for c in cols):
        await db.execute("UPDATE exercise_library SET builtin = 0 WHERE category = 'CrossFit'")
