"""Structured training-session writes for REST clients.

The form path captures a free-text note and lets the paid parser propose
entries for review. An API client (an agent that already talked the workout
through with the user) sends the entries itself, so the session and its sets
are written in one transaction with no parse step.

Every named movement must resolve through the exercise library or its
aliases, exactly like the confirm screen. Unlike that screen, an unknown name refuses the whole write
and reports the names: an agent can add them to the library and retry, while a
silently dropped set would be lost history.
"""

from app.i18n import N_, LocalizedError
from app.services.movement_vocab import EQUIPMENT, valid_equipment
from app.services.wod_movements import resolve_movement

# Server-side sanity bounds, shared with the form routes.
REPS_MAX = 1000
WEIGHT_KG_MAX = 1000.0
DURATION_MINUTES_MAX = 1440.0
DURATION_SECONDS_MAX = 86400.0
SET_NUMBER_MAX = 100
MAX_ENTRIES = 200
NOTES_MAX = 4000
ENTRY_NOTE_MAX = 200
TOKEN_PREFIX = "api:"


class TrainingWriteError(LocalizedError):
    labels = {
        "set_number": N_("Set number"),
        "reps": N_("Reps"),
        "weight": N_("Weight"),
        "duration": N_("Duration"),
        "equipment": N_("Equipment"),
    }

    def __init__(self, status: int, message: str, unknown_movements: list[str] | None = None, **params):
        super().__init__(message, **params)
        self.status = status
        self.unknown_movements = unknown_movements or []

    def for_one_set(self) -> "TrainingWriteError":
        """The same failure about one set: "reps must be ...", not "entries[0].reps must be ..."."""
        params = {key: value for key, value in self.params.items() if key != "row"}
        return TrainingWriteError(
            self.status, self.template.replace("entries[{row}].", ""), self.unknown_movements, **params
        )


# The one-set wording of the per-entry messages, for extraction; the rest are
# shared with other forms ("{field} must be between {low} and {high}").
_ONE_SET_MESSAGES = (N_("movement is required"), N_("equipment must be one of: {choices}"))


def _check_range(value, low, high, field: str, row: int):
    if value is None:
        return None
    if isinstance(value, bool) or not low <= value <= high:
        raise TrainingWriteError(
            422, N_("entries[{row}].{field} must be between {low} and {high}"), row=row, field=field, low=low, high=high
        )
    return value


def _validate_entries(entries: list[dict]) -> list[tuple]:
    if len(entries) > MAX_ENTRIES:
        raise TrainingWriteError(422, N_("At most {limit} entries per session"), limit=MAX_ENTRIES)
    rows = []
    for i, entry in enumerate(entries):
        movement = (entry.get("movement") or "").strip()
        if not movement:
            raise TrainingWriteError(422, N_("entries[{row}].movement is required"), row=i)
        set_number = _check_range(entry.get("set_number", 1), 1, SET_NUMBER_MAX, "set_number", i) or 1
        reps = _check_range(entry.get("reps"), 0, REPS_MAX, "reps", i)
        weight = _check_range(entry.get("weight"), 0, WEIGHT_KG_MAX, "weight", i)
        duration = _check_range(entry.get("duration"), 0, DURATION_SECONDS_MAX, "duration", i)
        note = (entry.get("note") or "")[:ENTRY_NOTE_MAX]
        equipment = entry.get("equipment") or None
        if not valid_equipment(equipment):
            raise TrainingWriteError(
                422, N_("entries[{row}].equipment must be one of: {choices}"), row=i, choices=", ".join(EQUIPMENT)
            )
        rows.append((movement, set_number, reps, weight, duration, note, equipment))
    return rows


async def _write_entries(db, session_id: int, rows: list[tuple]) -> int:
    resolved = []
    unknown = []
    for movement, set_number, reps, weight, duration, note, equipment in rows:
        exercise_id = await resolve_movement(db, movement)
        if exercise_id is None:
            unknown.append(movement)
            continue
        resolved.append((session_id, exercise_id, set_number, reps, weight, duration, note, equipment))
    if unknown:
        names = sorted(set(unknown))
        raise TrainingWriteError(
            422,
            N_("Movements outside the exercise library: {names}. Add them to the library, then retry."),
            unknown_movements=names,
            names=", ".join(names),
        )
    await db.executemany(
        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration, notes, equipment) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        resolved,
    )
    return len(resolved)


def _check_duration(duration_minutes):
    if duration_minutes is not None and (
        isinstance(duration_minutes, bool) or not 1 <= duration_minutes <= DURATION_MINUTES_MAX
    ):
        raise TrainingWriteError(
            422, N_("duration_minutes must be between 1 and {high}"), high=int(DURATION_MINUTES_MAX)
        )


async def _begin(db) -> None:
    if db.in_transaction:
        raise RuntimeError("Training writes require a connection without pending writes")
    await db.execute("BEGIN IMMEDIATE")


async def create_session(
    db,
    *,
    day: str,
    duration_minutes: int | None,
    notes: str,
    entries: list[dict],
    idempotency_key: str | None = None,
) -> tuple[dict, bool]:
    """Write a session with its entries. Returns (session summary, created).

    A reused idempotency key with the same date and note replays the first
    session; with different content it is a conflict, not a second session.
    """
    _check_duration(duration_minutes)
    notes = (notes or "").strip()[:NOTES_MAX]
    rows = _validate_entries(entries)
    token = f"{TOKEN_PREFIX}{idempotency_key}" if idempotency_key else None
    await _begin(db)
    try:
        if token:
            owner = await db.execute_fetchall(
                "SELECT id, date, notes FROM training_sessions WHERE capture_token = ?", (token,)
            )
            if owner:
                await db.rollback()
                if owner[0]["date"] == day and (owner[0]["notes"] or "") == notes:
                    return await session_summary(db, owner[0]["id"]), False
                raise TrainingWriteError(409, N_("idempotency_key was already used for a different session"))
        cursor = await db.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, capture_token) VALUES (?, ?, ?, ?)",
            (day, duration_minutes, notes, token),
        )
        session_id = cursor.lastrowid
        await _write_entries(db, session_id, rows)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return await session_summary(db, session_id), True


async def update_session(db, session_id: int, changes: dict) -> dict:
    """Change date, duration or note; `entries`, when given, replaces every set.

    Replacing the sets also settles a pending parse: confirming it later would
    write a second set of entries on top of the ones sent here.
    """
    if "duration_minutes" in changes:
        _check_duration(changes["duration_minutes"])
    rows = _validate_entries(changes["entries"]) if changes.get("entries") is not None else None
    await _begin(db)
    try:
        existing = await db.execute_fetchall("SELECT id FROM training_sessions WHERE id = ?", (session_id,))
        if not existing:
            raise TrainingWriteError(404, N_("Training session not found"))
        fields = {}
        if "date" in changes:
            fields["date"] = changes["date"]
        if "duration_minutes" in changes:
            fields["duration_minutes"] = changes["duration_minutes"]
        if "notes" in changes:
            fields["notes"] = (changes["notes"] or "").strip()[:NOTES_MAX]
        if rows is not None:
            fields["wod_parsed"] = None
        if fields:
            assignments = ", ".join(f"{key} = ?" for key in fields)
            await db.execute(f"UPDATE training_sessions SET {assignments} WHERE id = ?", (*fields.values(), session_id))
        if rows is not None:
            await db.execute("DELETE FROM training_entries WHERE session_id = ?", (session_id,))
            await _write_entries(db, session_id, rows)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return await session_summary(db, session_id)


async def update_entry(db, entry_id: int, entry: dict) -> str:
    """Correct one stored set, with the same bounds and movement rules as a new one.

    Returns the session's date, so the caller can go back to that day.
    """
    try:
        movement, set_number, reps, weight, duration, note, equipment = _validate_entries([entry])[0]
    except TrainingWriteError as exc:
        raise exc.for_one_set() from exc
    await _begin(db)
    try:
        rows = await db.execute_fetchall(
            "SELECT ts.date, te.exercise_id, tex.name FROM training_entries te "
            "JOIN training_sessions ts ON ts.id = te.session_id "
            "JOIN training_exercises tex ON tex.id = te.exercise_id WHERE te.id = ?",
            (entry_id,),
        )
        if not rows:
            raise TrainingWriteError(404, N_("That set no longer exists"))
        # An unchanged movement keeps its row: resolving it again could
        # reactivate an archived movement as a side effect of fixing a note.
        if movement.lower() == rows[0]["name"].lower():
            exercise_id = rows[0]["exercise_id"]
        else:
            exercise_id = await resolve_movement(db, movement)
        if exercise_id is None:
            raise TrainingWriteError(
                422,
                N_("{movement} is not in the exercise library. Add it there, then correct the set."),
                [movement],
                movement=movement,
            )
        await db.execute(
            "UPDATE training_entries SET exercise_id = ?, set_number = ?, reps = ?, weight = ?, duration = ?, "
            "notes = ?, equipment = ? WHERE id = ?",
            (exercise_id, set_number, reps, weight, duration, note, equipment, entry_id),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return rows[0]["date"]


async def delete_entry(db, entry_id: int) -> str:
    """Delete one stored set; returns its session's date. The session stays, even if now empty."""
    rows = await db.execute_fetchall(
        "SELECT ts.date FROM training_entries te JOIN training_sessions ts ON ts.id = te.session_id WHERE te.id = ?",
        (entry_id,),
    )
    if not rows:
        raise TrainingWriteError(404, N_("That set no longer exists"))
    await db.execute("DELETE FROM training_entries WHERE id = ?", (entry_id,))
    await db.commit()
    return rows[0]["date"]


async def delete_session(db, session_id: int) -> None:
    cursor = await db.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    await db.commit()
    if cursor.rowcount == 0:
        raise TrainingWriteError(404, N_("Training session not found"))


async def session_summary(db, session_id: int) -> dict:
    sessions = await db.execute_fetchall(
        "SELECT id, date, duration_minutes, notes FROM training_sessions WHERE id = ?", (session_id,)
    )
    entries = await db.execute_fetchall(
        "SELECT ex.name AS movement, e.set_number, e.reps, e.weight, e.duration, e.equipment, e.notes AS note "
        "FROM training_entries e JOIN training_exercises ex ON ex.id = e.exercise_id "
        "WHERE e.session_id = ? ORDER BY e.id",
        (session_id,),
    )
    summary = dict(sessions[0])
    summary["entries"] = [dict(row) for row in entries]
    return summary
