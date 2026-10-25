"""Movement vocabulary beyond the library name: aliases, families, equipment.

Matching a written movement is exact library name, then alias, then unknown.
There is no nearest match: a gap in the vocabulary has to surface, because a
guessed movement corrupts history silently.
"""

from app.i18n import N_
from app.library_validation import LibraryWriteError

# Mirrors the CHECK constraint written by migration 033.
EQUIPMENT = ("barbell", "dumbbell", "kettlebell", "plate", "bodyweight", "ball", "band", "machine", "other")
ALIAS_MAX = 80
MAX_ALIASES_PER_ENTRY = 20


def valid_equipment(value) -> bool:
    return value is None or value in EQUIPMENT


async def canonical_name(db, written: str) -> str | None:
    """The active library name for `written`, by exact name or alias."""
    clean = (written or "").strip()
    if not clean:
        return None
    rows = await db.execute_fetchall(
        "SELECT name FROM exercise_library WHERE lower(name) = lower(?) AND archived = 0", (clean,)
    )
    if rows:
        return rows[0]["name"]
    rows = await db.execute_fetchall(
        "SELECT l.name FROM exercise_aliases a JOIN exercise_library l ON l.id = a.library_id "
        "WHERE a.alias = ? COLLATE NOCASE AND l.archived = 0",
        (clean,),
    )
    return rows[0]["name"] if rows else None


async def aliases_by_library_id(db, ids: list[int] | None = None) -> dict[int, list[str]]:
    sql = "SELECT library_id, alias FROM exercise_aliases"
    params: list = []
    if ids is not None:
        if not ids:
            return {}
        sql += f" WHERE library_id IN ({','.join('?' * len(ids))})"
        params = list(ids)
    out: dict[int, list[str]] = {}
    for row in await db.execute_fetchall(sql, params):
        out.setdefault(row["library_id"], []).append(row["alias"])
    return {key: sorted(values, key=str.lower) for key, values in out.items()}


def normalize_aliases(raw: list[str]) -> list[str]:
    out: dict[str, str] = {}
    for item in raw:
        alias = " ".join(str(item).split())
        if not alias:
            continue
        if len(alias) > ALIAS_MAX:
            raise LibraryWriteError(
                422, N_("alias {alias!r} exceeds {ALIAS_MAX} characters"), alias=alias, ALIAS_MAX=ALIAS_MAX
            )
        out.setdefault(alias.lower(), alias)
    if len(out) > MAX_ALIASES_PER_ENTRY:
        raise LibraryWriteError(
            422,
            N_("at most {MAX_ALIASES_PER_ENTRY} aliases allowed per entry, got {value}"),
            MAX_ALIASES_PER_ENTRY=MAX_ALIASES_PER_ENTRY,
            value=len(out),
        )
    return sorted(out.values(), key=str.lower)


async def write_classification(db, entry_id: int, changes: dict) -> set[str]:
    """Apply family / default_equipment / aliases inside the caller's transaction.

    These classify a movement rather than redefine it, so they stay editable on
    builtin entries, like tags.
    """
    changed: set[str] = set()
    if "family" in changes:
        family = changes["family"]
        family_id = None
        if family:
            rows = await db.execute_fetchall(
                "SELECT id FROM exercise_families WHERE name = ? COLLATE NOCASE", (family.strip(),)
            )
            if not rows:
                names = [
                    r["name"]
                    for r in await db.execute_fetchall("SELECT name FROM exercise_families ORDER BY display_order")
                ]
                raise LibraryWriteError(422, N_("family must be one of: {choices}"), choices=", ".join(names))
            family_id = rows[0]["id"]
        await db.execute("UPDATE exercise_library SET family_id = ? WHERE id = ?", (family_id, entry_id))
        changed.add("family")
    if "default_equipment" in changes:
        equipment = changes["default_equipment"] or None
        if not valid_equipment(equipment):
            raise LibraryWriteError(
                422, N_("default_equipment must be one of: {choices}"), choices=", ".join(EQUIPMENT)
            )
        await db.execute("UPDATE exercise_library SET default_equipment = ? WHERE id = ?", (equipment, entry_id))
        changed.add("default_equipment")
    if "aliases" in changes:
        aliases = normalize_aliases(changes["aliases"] or [])
        names = {r["name"].lower() for r in await db.execute_fetchall("SELECT name FROM exercise_library")}
        for alias in aliases:
            if alias.lower() in names:
                raise LibraryWriteError(409, N_("alias {alias!r} is already a library name"), alias=alias)
            owner = await db.execute_fetchall(
                "SELECT library_id FROM exercise_aliases WHERE alias = ? COLLATE NOCASE AND library_id != ?",
                (alias, entry_id),
            )
            if owner:
                raise LibraryWriteError(
                    409, N_("alias {alias!r} already belongs to library entry {value}"), alias=alias, value=owner[0][0]
                )
        await db.execute("DELETE FROM exercise_aliases WHERE library_id = ?", (entry_id,))
        await db.executemany(
            "INSERT INTO exercise_aliases (alias, library_id) VALUES (?, ?)", [(alias, entry_id) for alias in aliases]
        )
        changed.add("aliases")
    return changed
