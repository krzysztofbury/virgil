"""Concurrent read-merge-write paths keep both changes.

Each of these reads a row, merges the supplied fields and writes the whole row
back. Two API or MCP writes to the same record used to interleave: both read the
old row, and the second write put back the field the first one had just changed.
"""

import asyncio

import pytest


class _PausesAfterFirstRead:
    """A connection that stops between its first read and its write.

    The other writer gets the gap. Without a transaction it reads and commits
    in it, and this writer then overwrites that change with what it read before.
    """

    def __init__(self, db, have_read: asyncio.Event):
        self._db = db
        self._have_read = have_read

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def execute_fetchall(self, sql, parameters=()):
        rows = await self._db.execute_fetchall(sql, parameters)
        if not self._have_read.is_set():
            self._have_read.set()
            await asyncio.sleep(0.3)
        return rows


async def _seed(db):
    await db.execute(
        "INSERT INTO habits (slug, name, slip_label, slip_definition, amount_unit, slip_threshold, near_miss_label, "
        "start_date, tracked) VALUES ('sugar', 'Sugar', 'I ate it', '', 'grams', 10, '', '2026-01-01', 1)"
    )
    habit_id = (await db.execute_fetchall("SELECT id FROM habits WHERE slug = 'sugar'"))[0][0]
    await db.execute(
        "INSERT INTO feniks_bricks (habit_id, date, hook, craving, story) VALUES (?, '2026-10-01', 'hook', 3, 'story')",
        (habit_id,),
    )
    await db.execute("INSERT INTO body_measurements (date, weight, waist) VALUES ('2026-10-01', 80, 90)")
    await db.commit()
    return (await db.execute_fetchall("SELECT id FROM feniks_bricks"))[0][0]


async def _measurements(db, key, change):
    from app.services.measurements import upsert_measurements

    await upsert_measurements(db, "2026-10-01", change)


async def _habit(db, key, change):
    from app.services.habits import update_habit

    await update_habit(db, "sugar", change)


async def _brick(db, key, change):
    from app.services.freedom_log import update_brick

    await update_brick(db, key, change)


CASES = [
    (
        _measurements,
        {"weight": 81.0},
        {"waist": 88.0},
        "SELECT weight, waist FROM body_measurements WHERE date = '2026-10-01'",
        (81.0, 88.0),
    ),
    (
        _habit,
        {"target_days": 30},
        {"big_why": "health"},
        "SELECT target_days, big_why FROM habits WHERE slug = 'sugar'",
        (30, "health"),
    ),
    (_brick, {"craving": 7}, {"story": "new story"}, "SELECT craving, story FROM feniks_bricks", (7, "new story")),
]


@pytest.mark.parametrize(
    ("write", "first", "second", "query", "expected"), CASES, ids=["measurements", "habit", "brick"]
)
def test_two_concurrent_writes_to_one_record_both_land(tmp_path, monkeypatch, write, first, second, query, expected):
    import app.user_db as user_db

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))

    async def scenario():
        await user_db.create_user_db("concurrent.db")
        slow = await user_db.open_user_db("concurrent.db")
        fast = await user_db.open_user_db("concurrent.db")
        try:
            brick_id = await _seed(slow)
            have_read = asyncio.Event()

            async def second_writer():
                await have_read.wait()
                await write(fast, brick_id, second)

            await asyncio.gather(write(_PausesAfterFirstRead(slow, have_read), brick_id, first), second_writer())
            return tuple(await fast.execute_fetchall(query))[0]
        finally:
            await slow.close()
            await fast.close()

    assert tuple(asyncio.run(scenario())) == expected
