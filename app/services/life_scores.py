"""Life score assessments, shared by the dashboard form, REST and MCP.

One row per date. A write merges: fields absent from `changes` keep their stored
value, and an explicit None clears one. The power level is what the person
declares, never computed from the areas.

The same table still holds metrics older forms stored beside an assessment
(weight, waist, Freedom status, audience, revenue); only reads of those remain.
"""

from app.db import LIFE_AREA_LABELS, LIFE_AREAS
from app.i18n import N_, LocalizedError

TEXT_FIELDS = ("diagnostic", "priorities")
FIELDS = (*LIFE_AREAS, "power_level", *TEXT_FIELDS)
TEXT_MAX_LENGTH = 2000
SCORED = " OR ".join(f"{column} IS NOT NULL" for column in ("power_level", *LIFE_AREAS))
# A row holding only metrics from an older form is a measurement, not an assessment.
# COALESCE because a restored row can hold NULL text, which the app never writes.
ASSESSED = " OR ".join((SCORED, *(f"COALESCE({column}, '') != ''" for column in TEXT_FIELDS)))
_DROP_EMPTY = (
    "DELETE FROM life_scores WHERE date = ? AND weight IS NULL AND waist IS NULL AND energy_avg IS NULL "
    "AND linkedin_followers IS NULL AND youtube_views IS NULL AND revenue IS NULL "
    f"AND COALESCE(pmo_status, '') = '' AND NOT ({ASSESSED})"  # noqa: S608
)


class LifeScoreError(LocalizedError):
    labels = {
        **dict(zip(LIFE_AREAS, LIFE_AREA_LABELS, strict=True)),
        "power_level": N_("Power level"),
        "diagnostic": N_("Diagnostics"),
        "priorities": N_("Priorities"),
    }


def _validate(changes: dict) -> dict:
    unknown = set(changes) - set(FIELDS)
    if unknown:
        raise LifeScoreError(N_("Unknown life score fields: {fields}"), fields=", ".join(sorted(unknown)))
    if not changes:
        raise LifeScoreError(N_("Enter at least one score or note."))
    clean = {}
    for field, value in changes.items():
        if field in TEXT_FIELDS:
            # A browser submits a line break as two characters but counts it as one.
            clean[field] = (value or "").replace("\r\n", "\n").strip()
            if len(clean[field]) > TEXT_MAX_LENGTH:
                raise LifeScoreError(N_("{field} is too long"), field=field)
        elif value is None:
            clean[field] = None
        elif field == "power_level":
            if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 10:
                raise LifeScoreError(N_("{field} must be between {low} and {high}"), field=field, low="0", high="10")
            clean[field] = float(value)
        else:
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10:
                raise LifeScoreError(N_("{field} must be a whole number from 1 to 10"), field=field)
            clean[field] = value
    return clean


def _assessment(row) -> dict:
    return {"date": row["date"], **{field: row[field] for field in FIELDS}}


async def upsert_life_scores(db, day: str, changes: dict) -> dict:
    clean = _validate(changes)
    columns = ", ".join(clean)
    # Column names come from FIELDS through _validate, never from the caller.
    await db.execute(
        f"INSERT INTO life_scores (date, {columns}) VALUES (?{', ?' * len(clean)}) "  # noqa: S608
        f"ON CONFLICT(date) DO UPDATE SET {', '.join(f'{column} = excluded.{column}' for column in clean)}",
        (day, *clean.values()),
    )
    await db.execute(_DROP_EMPTY, (day,))
    await db.commit()
    rows = await db.execute_fetchall("SELECT * FROM life_scores WHERE date = ?", (day,))
    if rows:
        return _assessment(rows[0])
    return {"date": day, **dict.fromkeys(FIELDS), **dict.fromkeys(TEXT_FIELDS, "")}


async def list_life_scores(db, since: str) -> list[dict]:
    rows = await db.execute_fetchall(
        f"SELECT * FROM life_scores WHERE date >= ? AND ({ASSESSED}) ORDER BY date DESC",  # noqa: S608
        (since,),
    )
    return [_assessment(row) for row in rows]


async def delete_life_scores(db, day: str) -> bool:
    """Remove the assessment of a date. A row that older forms also filled with
    metrics keeps them, since the dashboard and exports still read those."""
    cleared = ", ".join(
        (
            *(f"{column} = NULL" for column in ("power_level", *LIFE_AREAS)),
            *(f"{column} = ''" for column in TEXT_FIELDS),
        )
    )
    cursor = await db.execute(
        f"UPDATE life_scores SET {cleared} WHERE date = ? AND ({ASSESSED})",  # noqa: S608
        (day,),
    )
    found = cursor.rowcount > 0
    await db.execute(_DROP_EMPTY, (day,))
    await db.commit()
    return found
