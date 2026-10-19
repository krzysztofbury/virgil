"""Streak and weekly clean rate belong to one habit.

Both read relapse markers from pmo_events. Ignoring which habit a marker belongs
to, or never subtracting relapses from the week, passed the suite: the existing
tests only checked that the keys were there.
"""

import asyncio
from datetime import date

import pytest

# Wednesday: the week so far is Monday 12th to today, three days.
TODAY = date(2026, 10, 14)


class _Today(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


@pytest.fixture
def two_habits(tmp_path, monkeypatch):
    """Sugar slipped on the 8th and on Tuesday 13th; Coffee never slipped."""
    import app.services.streak as streak
    import app.user_db as user_db
    from app.services import freedom_log

    monkeypatch.setattr(user_db, "USERS_DB_DIR", str(tmp_path))
    monkeypatch.setattr(streak, "date", _Today)

    async def seed():
        await user_db.create_user_db("streak.db")
        db = await user_db.open_user_db("streak.db")
        for slug, start in (("sugar", "2026-09-01"), ("coffee", "2026-10-01")):
            await db.execute(
                "INSERT INTO habits (slug, name, slip_label, start_date) VALUES (?, ?, 'I slipped', ?)",
                (slug, slug.capitalize(), start),
            )
        await db.commit()
        habits = {row["slug"]: dict(row) for row in await db.execute_fetchall("SELECT * FROM habits")}
        for day in ("2026-10-08", "2026-10-13"):
            await freedom_log.save_day(
                db, habits["sugar"], day, slipped=True, amount=None, near_miss=False, note="slip"
            )
        return db, {slug: habit["id"] for slug, habit in habits.items()}

    db, ids = asyncio.run(seed())
    yield db, ids
    asyncio.run(db.close())


def test_the_streak_counts_only_the_habits_own_relapses(two_habits):
    from app.services.streak import get_streak

    db, ids = two_habits

    async def streaks():
        return await get_streak(db, ids["sugar"]), await get_streak(db, ids["coffee"])

    sugar, coffee = asyncio.run(streaks())

    assert sugar == (1, date(2026, 10, 13))
    assert coffee == (13, None)


def test_the_weekly_clean_rate_subtracts_only_this_weeks_relapses_of_the_habit(two_habits):
    from app.services.streak import get_week_clean

    db, ids = two_habits

    async def weeks():
        return await get_week_clean(db, ids["sugar"]), await get_week_clean(db, ids["coffee"])

    sugar, coffee = asyncio.run(weeks())

    # The slip on the 8th was last week; Tuesday's costs one of three days.
    assert sugar == (2, 3, 67)
    assert coffee == (3, 3, 100)
