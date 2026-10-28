"""Set near_miss on Freedom days that were already logged, and change nothing else.

Does not change slipped, amount, note, bricks, or relapse markers, and does not
create a day that was never logged. Prints the plan and writes nothing unless
--apply is given. If any date has no day entry, nothing is written.

The days 2026-10-26 and 2026-10-27 were logged with no porn and notes about
edging while near_miss stayed 0. After reading the dry-run:

    python scripts/correct_freedom_near_miss.py --database data/users/<file>.db \\
        --habit porn --date 2026-10-26 --date 2026-10-27
    python scripts/correct_freedom_near_miss.py --apply --database data/users/<file>.db \\
        --habit porn --date 2026-10-26 --date 2026-10-27

Stop writers first, or accept that a save landing during the run can overwrite
this correction.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite  # noqa: E402

from app.services.freedom_log import FreedomWriteError, correct_near_miss, past_or_today  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="One user's SQLite file")
    parser.add_argument("--habit", required=True, help="Habit slug, for example porn")
    parser.add_argument("--date", action="append", required=True, dest="dates", help="YYYY-MM-DD, repeatable")
    parser.add_argument("--apply", action="store_true", help="Write. Without this, the command only prints the plan")
    return parser


async def _load(database: Path, habit_slug: str, dates: list[str]) -> tuple[aiosqlite.Connection, dict, list[dict]]:
    if not database.is_file():
        raise FreedomWriteError(f"No database at {database}.")
    db = await aiosqlite.connect(database)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    habits = await db.execute_fetchall("SELECT * FROM habits WHERE slug = ?", (habit_slug,))
    if not habits:
        await db.close()
        raise FreedomWriteError(f"No habit {habit_slug!r}.")
    habit = dict(habits[0])
    if not habit["near_miss_label"]:
        await db.close()
        raise FreedomWriteError("This habit does not record a near miss.")
    plan = []
    for day in dates:
        entry = past_or_today(day)
        if entry is None:
            await db.close()
            raise FreedomWriteError(f"{day} is not a date of today or earlier.")
        rows = await db.execute_fetchall(
            "SELECT slipped, amount, near_miss, note FROM feniks_daily WHERE habit_id = ? AND date = ?",
            (habit["id"], entry),
        )
        if not rows:
            await db.close()
            raise FreedomWriteError(f"No day entry for {entry}. Nothing was written.")
        plan.append({"date": entry, **dict(rows[0])})
    return db, habit, plan


async def _run(database: Path, habit_slug: str, dates: list[str], apply: bool) -> int:
    db, habit, plan = await _load(database, habit_slug, dates)
    try:
        for row in plan:
            note = (row["note"] or "").replace("\n", " ")[:80]
            print(
                f"{row['date']}  near_miss {row['near_miss']} -> 1  slipped={row['slipped']}  "
                f"amount={row['amount']!r}  note={note!r}"
            )
        if not apply:
            print("Dry run. Nothing was written.")
            return 0
        # The reads above can leave a transaction open. The correction refuses
        # a connection that already has one.
        await db.commit()
        for row in plan:
            result = await correct_near_miss(db, habit, row["date"])
            print(f"{row['date']}  {'updated' if result['changed'] else 'unchanged'}")
        return 0
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(args.database, args.habit, args.dates, args.apply))
    except FreedomWriteError as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
