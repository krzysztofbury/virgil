"""Make training exercise names unique and merge legacy race duplicates."""

import aiosqlite


async def up(db: aiosqlite.Connection) -> None:
    columns = {column["name"] for column in await db.execute_fetchall("PRAGMA table_info(training_exercises)")}
    if not columns:
        return

    duplicate_groups = await db.execute_fetchall(
        """SELECT MIN(id) AS keeper_id,
                  MIN(archived) AS archived,
                  MIN(ad_hoc) AS ad_hoc,
                  name
           FROM training_exercises
           GROUP BY name COLLATE NOCASE
           HAVING COUNT(*) > 1"""
    )
    for group in duplicate_groups:
        duplicate_rows = await db.execute_fetchall(
            "SELECT * FROM training_exercises WHERE name = ? COLLATE NOCASE ORDER BY id",
            (group["name"],),
        )
        keeper_id = group["keeper_id"]
        semantic_fields = ("section", "target_sets", "target_reps", "metric")
        expected_semantics = tuple(duplicate_rows[0][field] for field in semantic_fields)
        if any(tuple(row[field] for field in semantic_fields) != expected_semantics for row in duplicate_rows[1:]):
            duplicate_ids = [row["id"] for row in duplicate_rows]
            raise RuntimeError(f"training exercise duplicates have different semantics: ids={duplicate_ids}")

        nonempty_notes = {row["notes"] for row in duplicate_rows if row["notes"]}
        if len(nonempty_notes) > 1:
            duplicate_ids = [row["id"] for row in duplicate_rows]
            raise RuntimeError(f"training exercise duplicates have different notes: ids={duplicate_ids}")
        merged_notes = next(iter(nonempty_notes), "")
        display_order = min(row["display_order"] for row in duplicate_rows)
        await db.execute(
            """UPDATE training_exercises
               SET archived = ?, ad_hoc = ?, notes = ?, display_order = ?
               WHERE id = ?""",
            (group["archived"], group["ad_hoc"], merged_notes, display_order, keeper_id),
        )
        for row in duplicate_rows:
            duplicate_id = row["id"]
            if duplicate_id == keeper_id:
                continue
            await db.execute(
                "UPDATE training_entries SET exercise_id = ? WHERE exercise_id = ?",
                (keeper_id, duplicate_id),
            )
            await db.execute("DELETE FROM training_exercises WHERE id = ?", (duplicate_id,))

    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_training_exercises_name_nocase "
        "ON training_exercises(name COLLATE NOCASE)"
    )
