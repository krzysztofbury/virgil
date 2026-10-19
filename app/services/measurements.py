"""Body measurements (weight in kg, circumferences in cm) shared by the form and REST.

One row per date. A write merges: fields absent from `changes` keep their
stored value, and an explicit None clears one.
"""

FIELDS = ("weight", "arm", "waist", "hips", "thighs")
BOUNDS = {
    "weight": (20.0, 400.0),
    "arm": (10.0, 100.0),
    "waist": (30.0, 250.0),
    "hips": (30.0, 250.0),
    "thighs": (20.0, 150.0),
}


class MeasurementError(ValueError):
    pass


def _validate(changes: dict) -> dict:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise MeasurementError(f"Unknown measurement fields: {', '.join(sorted(unknown))}")
    clean = {}
    for field, value in changes.items():
        if value is not None:
            low, high = BOUNDS[field]
            if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
                raise MeasurementError(f"{field} must be between {low:g} and {high:g}")
            value = float(value)
        clean[field] = value
    return clean


async def upsert_measurements(db, day: str, changes: dict) -> dict:
    clean = _validate(changes)
    if db.in_transaction:
        raise RuntimeError("Measurement writes require a connection without pending writes")
    # The read and the write share one transaction, so two concurrent merges into
    # the same day cannot both read the old row and drop each other's field.
    await db.execute("BEGIN IMMEDIATE")
    try:
        rows = await db.execute_fetchall("SELECT * FROM body_measurements WHERE date = ?", (day,))
        current = {field: rows[0][field] for field in FIELDS} if rows else dict.fromkeys(FIELDS)
        current.update(clean)
        await db.execute(
            "INSERT INTO body_measurements (date, weight, arm, waist, hips, thighs) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET weight = excluded.weight, arm = excluded.arm, waist = excluded.waist, "
            "hips = excluded.hips, thighs = excluded.thighs",
            (day, *(current[field] for field in FIELDS)),
        )
        stored = await db.execute_fetchall("SELECT * FROM body_measurements WHERE date = ?", (day,))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return dict(stored[0])


async def list_measurements(db, since: str) -> list[dict]:
    rows = await db.execute_fetchall("SELECT * FROM body_measurements WHERE date >= ? ORDER BY date DESC", (since,))
    return [dict(row) for row in rows]


async def delete_measurements(db, day: str) -> bool:
    cursor = await db.execute("DELETE FROM body_measurements WHERE date = ?", (day,))
    await db.commit()
    return cursor.rowcount > 0
