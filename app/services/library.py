"""Atomic exercise-library writes shared by forms and REST."""

from app.library_validation import LibraryWriteError, normalize_tags, validate_library_write
from app.services.movement_vocab import write_classification


async def write_library(
    db,
    *,
    op: str,
    entry_id: int | None = None,
    fields: dict | None = None,
    tags=None,
    classification: dict | None = None,
) -> dict:
    """Own one transaction, including collision checks and tag replacement.

    `classification` carries family, default_equipment and aliases; a key's
    absence leaves that attribute unchanged.
    """
    if db.in_transaction:
        raise RuntimeError("Library writes require a connection without pending writes")
    await db.execute("BEGIN IMMEDIATE")
    try:
        existing = None
        if op != "create":
            rows = await db.execute_fetchall("SELECT * FROM exercise_library WHERE id = ?", (entry_id,))
            if not rows:
                raise LibraryWriteError(404, "Library entry not found.")
            existing = dict(rows[0])
        fields = dict(fields or {})
        if op == "archive":
            target = 1 if fields.get("archived") else 0
            if existing["archived"] == target:
                state = "archived" if target else "active"
                raise LibraryWriteError(409, f"Library entry is already {state}.")
            fields = {"archived": target}
        values = await validate_library_write(
            db, op="update" if op == "archive" else op, entry_id=entry_id, existing=existing, fields=fields
        )
        normalized_tags = normalize_tags(tags) if tags is not None else None
        new_name = (values or {}).get("name")
        if new_name and await db.execute_fetchall(
            "SELECT 1 FROM exercise_aliases WHERE alias = ? COLLATE NOCASE AND library_id IS NOT ?",
            (new_name, entry_id),
        ):
            raise LibraryWriteError(409, f"{new_name!r} is already an alias of another movement")
        changed = set(values or {})
        if op == "create":
            cursor = await db.execute(
                "INSERT INTO exercise_library (section, name, sets, reps, notes, display_order, metric, builtin) "
                "VALUES (?, ?, ?, ?, ?, (SELECT COALESCE(MAX(display_order), 0) + 1 FROM exercise_library), ?, 0)",
                tuple(values[key] for key in ("section", "name", "sets", "reps", "notes", "metric")),
            )
            entry_id = cursor.lastrowid
        elif op == "delete":
            await db.execute("DELETE FROM exercise_library WHERE id = ?", (entry_id,))
        elif values:
            assignments = ", ".join(f"{key} = ?" for key in values)
            await db.execute(f"UPDATE exercise_library SET {assignments} WHERE id = ?", (*values.values(), entry_id))
        if normalized_tags is not None and op != "delete":
            await db.execute("DELETE FROM exercise_library_tags WHERE library_id = ?", (entry_id,))
            await db.executemany(
                "INSERT INTO exercise_library_tags (library_id, tag) VALUES (?, ?)",
                [(entry_id, tag) for tag in normalized_tags],
            )
            changed.add("tags")
        if classification and op != "delete":
            changed |= await write_classification(db, entry_id, classification)
        await db.commit()
        return {"id": entry_id, "updated": sorted(changed)}
    except BaseException:
        await db.rollback()
        raise
