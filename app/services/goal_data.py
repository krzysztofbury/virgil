"""Canonical validation and writes for goals and their one-off execution reps."""

import json
import sqlite3
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta

from app.validation import truncate

GOAL_STATUSES = ("active", "paused", "completed", "abandoned")
REP_PERIODS = ("day", "week", "month", "quarter", "year")
REP_STATUSES = ("pending", "completed", "carried", "skipped")
HORIZONS = ("1yr", "3yr", "10yr")
# A deleted goal can be restored for this long; then it and its history go.
GOAL_TRASH_DAYS = 30
# Changes kept per goal, newest first. Metadata only, so the bound is about
# relevance, not size.
GOAL_HISTORY_MAX = 20
# What an edit can change, in the words the history shows.
_TRACKED_FIELDS = {
    "area_id": "area",
    "horizon": "horizon",
    "content": "text",
    "status": "status",
    "start_date": "start date",
    "end_date": "end date",
    "display_order": "order",
    "parent_goal_id": "parent goal",
}


class GoalDataError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def canonical_date(value: str | None, field: str, *, required: bool = False) -> str | None:
    if value in (None, ""):
        if required:
            raise GoalDataError(f"{field} is required")
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise GoalDataError(f"{field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise GoalDataError(f"{field} must use YYYY-MM-DD")
    return value


def period_bounds(period: str, due_date: str) -> tuple[str, str]:
    """Return the canonical period containing due_date.

    Weeks are ISO calendar weeks: Monday through Sunday. This is the only week
    contract exposed to agents and goal-rep writers.
    """
    if period not in REP_PERIODS:
        raise GoalDataError(f"period must be one of: {', '.join(REP_PERIODS)}")
    due = date.fromisoformat(canonical_date(due_date, "due_date", required=True))
    if period == "day":
        start = end = due
    elif period == "week":
        start = due - timedelta(days=due.weekday())
        end = start + timedelta(days=6)
    elif period == "month":
        start = due.replace(day=1)
        end = due.replace(day=monthrange(due.year, due.month)[1])
    elif period == "quarter":
        first_month = ((due.month - 1) // 3) * 3 + 1
        start = due.replace(month=first_month, day=1)
        end_month = first_month + 2
        end = due.replace(month=end_month, day=monthrange(due.year, end_month)[1])
    else:
        start = due.replace(month=1, day=1)
        end = due.replace(month=12, day=31)
    return start.isoformat(), end.isoformat()


def normalize_content(value: str, field: str, limit: int = 2000) -> str:
    if not isinstance(value, str):
        raise GoalDataError(f"{field} must be text")
    normalized = truncate(value, limit).strip()
    if not normalized:
        raise GoalDataError(f"{field} is required")
    return normalized


def _validate_goal_fields(
    *,
    horizon: str,
    status: str,
    start_date: str | None,
    end_date: str | None,
) -> tuple[str | None, str | None]:
    if horizon not in HORIZONS:
        raise GoalDataError(f"horizon must be one of: {', '.join(HORIZONS)}")
    if status not in GOAL_STATUSES:
        raise GoalDataError(f"status must be one of: {', '.join(GOAL_STATUSES)}")
    start = canonical_date(start_date, "start_date")
    end = canonical_date(end_date, "end_date")
    if start and end and start > end:
        raise GoalDataError("start_date must not be after end_date")
    return start, end


async def create_goal(
    db,
    *,
    area_id: int,
    horizon: str,
    content: str,
    status: str = "active",
    start_date: str | None = None,
    end_date: str | None = None,
    active: int = 0,
    display_order: int = 0,
    source: str = "manual",
    source_ref: str = "",
    parent_goal_id: int | None = None,
) -> tuple[dict, bool]:
    content = normalize_content(content, "content")
    start_date, end_date = _validate_goal_fields(
        horizon=horizon,
        status=status,
        start_date=start_date,
        end_date=end_date,
    )
    area = await db.execute_fetchall("SELECT 1 FROM goal_areas WHERE id = ?", (area_id,))
    if not area:
        raise GoalDataError("Goal area not found", 404)
    if parent_goal_id is not None:
        parent = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (parent_goal_id,))
        if not parent:
            raise GoalDataError("Parent goal not found", 404)
    source = normalize_content(source, "source", 50)
    source_ref = truncate(source_ref.strip(), 200)
    active = 1 if active and status == "active" else 0

    def same_write(goal: dict) -> bool:
        return (
            goal["area_id"] == area_id
            and goal["horizon"] == horizon
            and goal["content"] == content
            and goal["status"] == status
            and goal["start_date"] == start_date
            and goal["end_date"] == end_date
            and goal["active"] == active
            and goal["parent_goal_id"] == parent_goal_id
        )

    if source_ref:
        existing = await db.execute_fetchall(
            "SELECT * FROM goals WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        )
        if existing:
            goal = dict(existing[0])
            if not same_write(goal):
                raise GoalDataError("Idempotency key already belongs to a different goal write", 409)
            return goal, False
    try:
        cursor = await db.execute(
            """INSERT INTO goals
               (area_id, horizon, content, active, display_order, status, start_date, end_date, source, source_ref,
                parent_goal_id, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       CASE WHEN ? = 'completed' THEN datetime('now') ELSE NULL END)""",
            (
                area_id,
                horizon,
                content,
                active,
                display_order,
                status,
                start_date,
                end_date,
                source,
                source_ref,
                parent_goal_id,
                status,
            ),
        )
    except sqlite3.IntegrityError as exc:
        existing = (
            await db.execute_fetchall("SELECT * FROM goals WHERE source = ? AND source_ref = ?", (source, source_ref))
            if source_ref
            else []
        )
        if existing and same_write(dict(existing[0])):
            return dict(existing[0]), False
        if existing:
            raise GoalDataError("Idempotency key already belongs to a different goal write", 409) from exc
        raise GoalDataError("Goal write conflicts with existing data", 409) from exc
    rows = await db.execute_fetchall("SELECT * FROM goals WHERE id = ?", (cursor.lastrowid,))
    await record_goal_change(db, cursor.lastrowid, "created", status_to=status)
    return dict(rows[0]), True


async def record_goal_change(
    db,
    goal_id: int,
    event: str,
    fields: tuple[str, ...] = (),
    status_from: str | None = None,
    status_to: str | None = None,
) -> None:
    """Add one entry to a goal's history and drop the oldest past GOAL_HISTORY_MAX."""
    await db.execute(
        "INSERT INTO goal_changes (goal_id, event, fields, status_from, status_to) VALUES (?, ?, ?, ?, ?)",
        (goal_id, event, ", ".join(fields), status_from, status_to),
    )
    await db.execute(
        "DELETE FROM goal_changes WHERE goal_id = ? AND id NOT IN "
        "(SELECT id FROM goal_changes WHERE goal_id = ? ORDER BY id DESC LIMIT ?)",
        (goal_id, goal_id, GOAL_HISTORY_MAX),
    )


async def goal_history(db, goal_ids) -> dict[int, list[dict]]:
    """Each goal's recorded changes, newest first."""
    ids = list(goal_ids)
    if not ids:
        return {}
    rows = await db.execute_fetchall(
        f"SELECT * FROM goal_changes WHERE goal_id IN ({', '.join('?' * len(ids))}) ORDER BY id DESC",  # noqa: S608
        ids,
    )
    history: dict[int, list[dict]] = {}
    for row in rows:
        change = dict(row)
        # Stored in UTC by SQLite; shown in the server's time zone, like every date.
        change["when"] = (
            datetime.fromisoformat(change["changed_at"]).replace(tzinfo=UTC).astimezone().strftime("%Y-%m-%d %H:%M")
        )
        history.setdefault(row["goal_id"], []).append(change)
    return history


async def update_goal(db, goal_id: int, changes: dict) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM goals WHERE id = ?", (goal_id,))
    if not rows:
        raise GoalDataError("Goal not found", 404)
    current = dict(rows[0])
    allowed = {
        "area_id",
        "horizon",
        "content",
        "status",
        "start_date",
        "end_date",
        "active",
        "display_order",
        "parent_goal_id",
    }
    unknown = set(changes) - allowed
    if unknown:
        raise GoalDataError(f"Unsupported goal fields: {', '.join(sorted(unknown))}")
    values = {**current, **changes}
    values["content"] = normalize_content(values["content"], "content")
    values["start_date"], values["end_date"] = _validate_goal_fields(
        horizon=values["horizon"],
        status=values["status"],
        start_date=values["start_date"],
        end_date=values["end_date"],
    )
    area = await db.execute_fetchall("SELECT 1 FROM goal_areas WHERE id = ?", (values["area_id"],))
    if not area:
        raise GoalDataError("Goal area not found", 404)
    if values["parent_goal_id"] is not None:
        if values["parent_goal_id"] == goal_id:
            raise GoalDataError("A goal cannot be its own parent")
        parent = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (values["parent_goal_id"],))
        if not parent:
            raise GoalDataError("Parent goal not found", 404)
    if values["status"] in ("completed", "abandoned") and current["status"] != values["status"]:
        pending = await db.execute_fetchall(
            "SELECT 1 FROM goal_reps WHERE goal_id = ? AND status = 'pending' LIMIT 1", (goal_id,)
        )
        if pending:
            raise GoalDataError("Complete, skip, or remove pending reps before closing this goal", 409)
    if values["status"] != "active":
        values["active"] = 0
    await db.execute(
        """WITH RECURSIVE descendants(id) AS (
               SELECT id FROM goals WHERE parent_goal_id = ?
               UNION
               SELECT g.id FROM goals g JOIN descendants d ON g.parent_goal_id = d.id
           )
           UPDATE goals SET area_id = ?, horizon = ?, content = ?, status = ?, start_date = ?, end_date = ?,
               active = ?, display_order = ?, parent_goal_id = ?,
               completed_at = CASE WHEN ? = 'completed' THEN COALESCE(completed_at, datetime('now')) ELSE NULL END,
               updated_at = datetime('now')
           WHERE id = ?
             AND (? IS NULL OR (
                 ? != id
                 AND EXISTS (SELECT 1 FROM goals parent WHERE parent.id = ?)
                 AND ? NOT IN (SELECT id FROM descendants)
             ))
             AND (? NOT IN ('completed', 'abandoned') OR NOT EXISTS (
                 SELECT 1 FROM goal_reps WHERE goal_id = ? AND status = 'pending'
             ))""",
        (
            goal_id,
            values["area_id"],
            values["horizon"],
            values["content"],
            values["status"],
            values["start_date"],
            values["end_date"],
            1 if values["active"] else 0,
            values["display_order"],
            values["parent_goal_id"],
            values["status"],
            goal_id,
            values["parent_goal_id"],
            values["parent_goal_id"],
            values["parent_goal_id"],
            values["parent_goal_id"],
            values["status"],
            goal_id,
        ),
    )
    changed = await db.execute_fetchall("SELECT changes() AS count")
    if changed[0]["count"] != 1:
        latest = await db.execute_fetchall("SELECT 1 FROM goals WHERE id = ?", (goal_id,))
        if not latest:
            raise GoalDataError("Goal not found", 404)
        if values["status"] in ("completed", "abandoned"):
            pending = await db.execute_fetchall(
                "SELECT 1 FROM goal_reps WHERE goal_id = ? AND status = 'pending' LIMIT 1", (goal_id,)
            )
            if pending:
                raise GoalDataError("Complete, skip, or remove pending reps before closing this goal", 409)
        raise GoalDataError("Goal hierarchy changed concurrently; retry the update", 409)
    updated = dict((await db.execute_fetchall("SELECT * FROM goals WHERE id = ?", (goal_id,)))[0])
    fields = tuple(label for field, label in _TRACKED_FIELDS.items() if updated[field] != current[field])
    if fields:
        await record_goal_change(
            db,
            goal_id,
            "edited",
            fields,
            *((current["status"], updated["status"]) if "status" in fields else (None, None)),
        )
    elif updated["active"] != current["active"]:
        await record_goal_change(db, goal_id, "focused" if updated["active"] else "unfocused")
    return updated


async def delete_goal_record(db, goal_id: int) -> None:
    """Delete a goal with no reps, keeping it restorable for GOAL_TRASH_DAYS.

    The row is copied out with the goals and experiments that pointed at it,
    since deleting it clears those links (ON DELETE SET NULL).
    """
    rows = await db.execute_fetchall("SELECT * FROM goals WHERE id = ?", (goal_id,))
    if not rows:
        raise GoalDataError("Goal not found", 404)
    goal = dict(rows[0])
    children = [r["id"] for r in await db.execute_fetchall("SELECT id FROM goals WHERE parent_goal_id = ?", (goal_id,))]
    experiments = [
        r["id"] for r in await db.execute_fetchall("SELECT id FROM experiments WHERE goal_id = ?", (goal_id,))
    ]
    cursor = await db.execute(
        "DELETE FROM goals WHERE id = ? AND NOT EXISTS (SELECT 1 FROM goal_reps WHERE goal_id = ?)",
        (goal_id, goal_id),
    )
    if cursor.rowcount != 1:
        raise GoalDataError("Goals with execution history cannot be deleted; mark the goal abandoned instead", 409)
    await db.execute(
        "INSERT OR REPLACE INTO deleted_goals (id, goal_json, child_goal_ids, experiment_ids) VALUES (?, ?, ?, ?)",
        (goal_id, json.dumps(goal), json.dumps(children), json.dumps(experiments)),
    )
    await record_goal_change(db, goal_id, "deleted", status_from=goal["status"])
    await purge_deleted_goals(db)


async def purge_deleted_goals(db) -> None:
    """Forget deleted goals past GOAL_TRASH_DAYS, with their history."""
    cutoff = f"-{GOAL_TRASH_DAYS} days"
    await db.execute(
        "DELETE FROM goal_changes WHERE goal_id IN "
        "(SELECT id FROM deleted_goals WHERE deleted_at < datetime('now', ?))",
        (cutoff,),
    )
    await db.execute("DELETE FROM deleted_goals WHERE deleted_at < datetime('now', ?)", (cutoff,))


async def list_deleted_goals(db) -> list[dict]:
    """Restorable goals, newest deletion first, with the fields the page shows."""
    rows = await db.execute_fetchall(
        "SELECT id, goal_json, deleted_at, date(deleted_at, ?) AS restorable_until "
        "FROM deleted_goals WHERE deleted_at >= datetime('now', ?) ORDER BY deleted_at DESC, id DESC",
        (f"+{GOAL_TRASH_DAYS} days", f"-{GOAL_TRASH_DAYS} days"),
    )
    goals = []
    for row in rows:
        goal = json.loads(row["goal_json"])
        goals.append(
            {
                "id": row["id"],
                "content": goal["content"],
                "horizon": goal["horizon"],
                "deleted_at": row["deleted_at"],
                "restorable_until": row["restorable_until"],
            }
        )
    return goals


async def restore_goal_record(db, goal_id: int) -> dict:
    """Put a deleted goal back under its own id, with the links its delete cleared.

    A link is restored only where nothing has replaced it since: a child goal or
    experiment that was linked elsewhere in the meantime keeps its new link.
    """
    rows = await db.execute_fetchall(
        "SELECT * FROM deleted_goals WHERE id = ? AND deleted_at >= datetime('now', ?)",
        (goal_id, f"-{GOAL_TRASH_DAYS} days"),
    )
    if not rows:
        raise GoalDataError("This goal can no longer be restored", 404)
    goal = json.loads(rows[0]["goal_json"])
    if not await db.execute_fetchall("SELECT 1 FROM goal_areas WHERE id = ?", (goal["area_id"],)):
        raise GoalDataError("The goal's area no longer exists", 409)
    if goal["parent_goal_id"] is not None and not await db.execute_fetchall(
        "SELECT 1 FROM goals WHERE id = ?", (goal["parent_goal_id"],)
    ):
        goal["parent_goal_id"] = None
    if goal["source_ref"] and await db.execute_fetchall(
        "SELECT 1 FROM goals WHERE source = ? AND source_ref = ?", (goal["source"], goal["source_ref"])
    ):
        # Its idempotency key was reused while it was deleted; the newer goal keeps it.
        goal["source_ref"] = ""
    columns = list(goal)
    await db.execute(
        f"INSERT INTO goals ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # noqa: S608
        [goal[column] for column in columns],
    )
    for child_id in json.loads(rows[0]["child_goal_ids"]):
        await db.execute(
            "UPDATE goals SET parent_goal_id = ? WHERE id = ? AND parent_goal_id IS NULL", (goal_id, child_id)
        )
    for experiment_id in json.loads(rows[0]["experiment_ids"]):
        await db.execute(
            "UPDATE experiments SET goal_id = ? WHERE id = ? AND goal_id IS NULL", (goal_id, experiment_id)
        )
    await db.execute("DELETE FROM deleted_goals WHERE id = ?", (goal_id,))
    await record_goal_change(db, goal_id, "restored", status_to=goal["status"])
    restored = await db.execute_fetchall("SELECT * FROM goals WHERE id = ?", (goal_id,))
    return dict(restored[0])


async def create_rep(
    db,
    *,
    goal_id: int,
    content: str,
    period: str,
    due_date: str,
    notes: str = "",
    source: str = "manual",
    source_ref: str = "",
    carried_from_id: int | None = None,
) -> tuple[dict, bool]:
    goal = await db.execute_fetchall("SELECT status, start_date, end_date FROM goals WHERE id = ?", (goal_id,))
    if not goal:
        raise GoalDataError("Goal not found", 404)
    if goal[0]["status"] not in ("active", "paused"):
        raise GoalDataError("Reps can only be added to active or paused goals", 409)
    content = normalize_content(content, "content")
    period_start, period_end = period_bounds(period, due_date)
    if goal[0]["start_date"] and due_date < goal[0]["start_date"]:
        raise GoalDataError("due_date is before the goal window")
    if goal[0]["end_date"] and due_date > goal[0]["end_date"]:
        raise GoalDataError("due_date is after the goal window")
    source = normalize_content(source, "source", 50)
    source_ref = truncate(source_ref.strip(), 200)

    def same_write(rep: dict) -> bool:
        return (
            rep["goal_id"] == goal_id
            and rep["content"] == content
            and rep["period"] == period
            and rep["due_date"] == due_date
            and rep["notes"] == truncate(notes, 2000)
        )

    if source_ref:
        existing = await db.execute_fetchall(
            "SELECT * FROM goal_reps WHERE source = ? AND source_ref = ?",
            (source, source_ref),
        )
        if existing:
            rep = dict(existing[0])
            if not same_write(rep):
                raise GoalDataError("Idempotency key already belongs to a different rep write", 409)
            return rep, False
    try:
        cursor = await db.execute(
            """INSERT INTO goal_reps
               (goal_id, content, period, period_start, period_end, due_date, notes, source, source_ref,
                carried_from_id)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
               FROM goals g
               WHERE g.id = ? AND g.status IN ('active', 'paused')
                 AND (g.start_date IS NULL OR ? >= g.start_date)
                 AND (g.end_date IS NULL OR ? <= g.end_date)""",
            (
                goal_id,
                content,
                period,
                period_start,
                period_end,
                due_date,
                truncate(notes, 2000),
                source,
                source_ref,
                carried_from_id,
                goal_id,
                due_date,
                due_date,
            ),
        )
    except sqlite3.IntegrityError as exc:
        existing = (
            await db.execute_fetchall(
                "SELECT * FROM goal_reps WHERE source = ? AND source_ref = ?", (source, source_ref)
            )
            if source_ref
            else []
        )
        if existing and same_write(dict(existing[0])):
            return dict(existing[0]), False
        if existing:
            raise GoalDataError("Idempotency key already belongs to a different rep write", 409) from exc
        if carried_from_id is not None:
            raise GoalDataError("Rep was already carried", 409) from exc
        raise GoalDataError("Goal rep write conflicts with existing data", 409) from exc
    if cursor.rowcount != 1:
        latest = await db.execute_fetchall("SELECT status, start_date, end_date FROM goals WHERE id = ?", (goal_id,))
        if not latest:
            raise GoalDataError("Goal not found", 404)
        if latest[0]["status"] not in ("active", "paused"):
            raise GoalDataError("Reps can only be added to active or paused goals", 409)
        if latest[0]["start_date"] and due_date < latest[0]["start_date"]:
            raise GoalDataError("due_date is before the goal window")
        if latest[0]["end_date"] and due_date > latest[0]["end_date"]:
            raise GoalDataError("due_date is after the goal window")
        raise GoalDataError("Goal changed concurrently; retry the rep write", 409)
    rows = await db.execute_fetchall("SELECT * FROM goal_reps WHERE id = ?", (cursor.lastrowid,))
    return dict(rows[0]), True


async def update_rep(db, rep_id: int, changes: dict) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM goal_reps WHERE id = ?", (rep_id,))
    if not rows:
        raise GoalDataError("Goal rep not found", 404)
    current = dict(rows[0])
    if current["status"] != "pending":
        raise GoalDataError("Only pending reps can be edited", 409)
    allowed = {"goal_id", "content", "period", "due_date", "notes"}
    unknown = set(changes) - allowed
    if unknown:
        raise GoalDataError(f"Unsupported rep fields: {', '.join(sorted(unknown))}")
    values = {**current, **changes}
    values["content"] = normalize_content(values["content"], "content")
    period_start, period_end = period_bounds(values["period"], values["due_date"])
    goal = await db.execute_fetchall(
        "SELECT status, start_date, end_date FROM goals WHERE id = ?", (values["goal_id"],)
    )
    if not goal:
        raise GoalDataError("Goal not found", 404)
    if goal[0]["status"] not in ("active", "paused"):
        raise GoalDataError("Reps can only be moved to active or paused goals", 409)
    if goal[0]["start_date"] and values["due_date"] < goal[0]["start_date"]:
        raise GoalDataError("due_date is before the goal window")
    if goal[0]["end_date"] and values["due_date"] > goal[0]["end_date"]:
        raise GoalDataError("due_date is after the goal window")
    cursor = await db.execute(
        """UPDATE goal_reps SET goal_id = ?, content = ?, period = ?, period_start = ?, period_end = ?,
           due_date = ?, notes = ?, updated_at = datetime('now')
           WHERE id = ? AND status = 'pending' AND EXISTS (
               SELECT 1 FROM goals g WHERE g.id = ? AND g.status IN ('active', 'paused')
                 AND (g.start_date IS NULL OR ? >= g.start_date)
                 AND (g.end_date IS NULL OR ? <= g.end_date)
           )""",
        (
            values["goal_id"],
            values["content"],
            values["period"],
            period_start,
            period_end,
            values["due_date"],
            truncate(values["notes"], 2000),
            rep_id,
            values["goal_id"],
            values["due_date"],
            values["due_date"],
        ),
    )
    if cursor.rowcount != 1:
        raise GoalDataError("Goal or rep changed concurrently; retry the update", 409)
    updated = await db.execute_fetchall("SELECT * FROM goal_reps WHERE id = ?", (rep_id,))
    return dict(updated[0])


async def delete_rep_record(db, rep_id: int) -> None:
    cursor = await db.execute(
        """DELETE FROM goal_reps
           WHERE id = ? AND status = 'pending' AND carried_from_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM goal_reps child WHERE child.carried_from_id = ?)""",
        (rep_id, rep_id),
    )
    if cursor.rowcount == 1:
        return
    rows = await db.execute_fetchall("SELECT 1 FROM goal_reps WHERE id = ?", (rep_id,))
    if not rows:
        raise GoalDataError("Goal rep not found", 404)
    raise GoalDataError("Only pending reps outside a carry chain can be deleted", 409)


async def transition_rep(
    db,
    rep_id: int,
    action: str,
    *,
    due_date: str | None = None,
    period: str | None = None,
) -> tuple[dict, dict | None]:
    rows = await db.execute_fetchall("SELECT * FROM goal_reps WHERE id = ?", (rep_id,))
    if not rows:
        raise GoalDataError("Goal rep not found", 404)
    rep = dict(rows[0])
    if rep["status"] != "pending":
        if rep["status"] == "completed" and action == "complete":
            return rep, None
        if rep["status"] == "skipped" and action == "skip":
            return rep, None
        if rep["status"] == "carried" and action == "carry":
            children = await db.execute_fetchall("SELECT * FROM goal_reps WHERE carried_from_id = ?", (rep_id,))
            if children:
                child = dict(children[0])
                requested_period = period or rep["period"]
                if due_date == child["due_date"] and requested_period == child["period"]:
                    return rep, child
        raise GoalDataError("Only pending reps can transition", 409)
    goal = await db.execute_fetchall("SELECT status FROM goals WHERE id = ?", (rep["goal_id"],))
    if not goal or goal[0]["status"] not in ("active", "paused"):
        raise GoalDataError("Reps on a closed goal cannot transition", 409)
    if action == "complete":
        cursor = await db.execute(
            "UPDATE goal_reps SET status = 'completed', completed_at = datetime('now'), updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (rep_id,),
        )
        if cursor.rowcount != 1:
            raise GoalDataError("Rep changed concurrently. Retry the transition.", 409)
        carried = None
    elif action == "skip":
        cursor = await db.execute(
            "UPDATE goal_reps SET status = 'skipped', completed_at = NULL, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (rep_id,),
        )
        if cursor.rowcount != 1:
            raise GoalDataError("Rep changed concurrently. Retry the transition.", 409)
        carried = None
    elif action == "carry":
        if not due_date:
            raise GoalDataError("due_date is required when carrying a rep")
        cursor = await db.execute(
            "UPDATE goal_reps SET status = 'carried', completed_at = NULL, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (rep_id,),
        )
        if cursor.rowcount != 1:
            raise GoalDataError("Rep changed concurrently. Retry the transition.", 409)
        carried, _ = await create_rep(
            db,
            goal_id=rep["goal_id"],
            content=rep["content"],
            period=period or rep["period"],
            due_date=due_date,
            notes=rep["notes"],
            carried_from_id=rep_id,
        )
    else:
        raise GoalDataError("action must be complete, carry, or skip")
    updated = await db.execute_fetchall("SELECT * FROM goal_reps WHERE id = ?", (rep_id,))
    return dict(updated[0]), carried
