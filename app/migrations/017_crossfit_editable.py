"""Make the seeded CrossFit movements user-editable.

016 seeded them with builtin = 1, which app/library_validation.py's
validate_library_write treats as "protected": update and delete both refuse
anything but 'archived', so the row could only be archived. The CrossFit
vocabulary is the user's to curate, and it also drives the WOD parser's
closed prompt — a movement they cannot remove is a movement the parser will
keep proposing.

016 was fixed too, for fresh installs; this migration exists because 016 is
already recorded in schema_migrations on databases that ran it.
"""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    await db.execute("UPDATE exercise_library SET builtin = 0 WHERE category = 'CrossFit'")
