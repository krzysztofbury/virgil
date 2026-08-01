import json
import logging
from dataclasses import asdict
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.main import templates
from app.services.wod_movements import resolve_movement
from app.services.wod_parser import canonical_movements, parse_wod
from app.user_db import get_user_db_from_request
from app.validation import truncate, valid_date

logger = logging.getLogger(__name__)

router = APIRouter()

# Server-side sanity bounds — the client can send anything.
REPS_MAX = 1000
WEIGHT_KG_MAX = 1000.0
DURATION_MINUTES_MAX = 1440.0
DURATION_SECONDS_MAX = 86400.0


def _parse_int_in_range(raw, minimum: int, maximum: int) -> int | None:
    """Parse a form value as int within [minimum, maximum]; None if invalid."""
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None
    if value < minimum or value > maximum:
        return None
    return value


class _ConfirmRejected(Exception):
    """A /training/wod/confirm field was present but failed validation.

    Distinct from "absent/blank", which is a normal unset value (e.g. no
    weight for a bodyweight movement). Silently coercing an out-of-range
    value to None or a clamped default is the exact shape this branch keeps
    reproducing (set_number ... or 1, an MCP `if v` filter, a settings no-op)
    — it lets the user's already-reviewed workout vanish or collide without a
    trace. Raising here aborts the whole submission instead, loudly, before
    anything is written.
    """


def _confirm_int(raw, minimum: int, maximum: int, field: str, row: int) -> int | None:
    """Strict per-row integer parse for the confirm screen.

    Blank/absent -> None (nothing was entered, that's fine). Present but not
    an integer, or outside [minimum, maximum] -> raises _ConfirmRejected
    naming the row and field, so B2 (out-of-range values silently discarded)
    cannot recur here.
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _ConfirmRejected(f"wpis {row + 1}: {field} „{raw}” nie jest liczbą całkowitą") from None
    if value < minimum or value > maximum:
        raise _ConfirmRejected(f"wpis {row + 1}: {field}={value} poza zakresem [{minimum}, {maximum}]")
    return value


def _confirm_float(raw, minimum: float, maximum: float, field: str, row: int) -> float | None:
    """Strict per-row float parse for the confirm screen. See `_confirm_int`."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise _ConfirmRejected(f"wpis {row + 1}: {field} „{raw}” nie jest liczbą") from None
    if value < minimum or value > maximum:
        raise _ConfirmRejected(f"wpis {row + 1}: {field}={value} poza zakresem [{minimum}, {maximum}]")
    return value


@router.get("/training", response_class=HTMLResponse)
async def training_page(request: Request):
    db = get_user_db_from_request(request)

    # training_exercises is no longer read here: the page has no protocol list
    # and no per-exercise log form. The table itself stays — training_entries
    # references it, so every logged set (past and future) hangs off it, and the
    # WOD parser keeps creating rows there via resolve_movement().
    sessions = await db.execute_fetchall("SELECT * FROM training_sessions ORDER BY date DESC LIMIT 20")
    sessions = [dict(s) for s in sessions]

    # Load all entries for visible sessions in one query
    if sessions:
        session_ids = [s["id"] for s in sessions]
        placeholders = ",".join("?" * len(session_ids))
        all_entries = await db.execute_fetchall(
            f"""SELECT te.*, tex.name as exercise_name, tex.section
               FROM training_entries te
               JOIN training_exercises tex ON te.exercise_id = tex.id
               WHERE te.session_id IN ({placeholders})
               ORDER BY tex.display_order, te.set_number""",
            session_ids,
        )
        entries_by_session: dict[int, list[dict]] = {}
        for e in all_entries:
            entries_by_session.setdefault(e["session_id"], []).append(dict(e))
        for s in sessions:
            s["entries"] = entries_by_session.get(s["id"], [])
    else:
        for s in sessions:
            s["entries"] = []

    # --- KPIs: This Week ---
    today = date.today()
    # Monday of current week
    monday = today - timedelta(days=today.weekday())
    monday_str = monday.isoformat()

    # Session count (simple count, no join inflation)
    session_count_row = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM training_sessions WHERE date >= ?",
        (monday_str,),
    )
    kpi_sessions = session_count_row[0]["cnt"] if session_count_row else 0

    # Volume (Core only) and total reps (all sections)
    week_stats = await db.execute_fetchall(
        """SELECT
               SUM(CASE WHEN tex.section = 'Core' AND tex.metric = 'reps'
                        THEN te.reps * COALESCE(te.weight, 0) ELSE 0 END) as core_volume,
               SUM(CASE WHEN tex.metric = 'reps' THEN te.reps ELSE 0 END) as total_reps
           FROM training_entries te
           JOIN training_sessions ts ON te.session_id = ts.id
           JOIN training_exercises tex ON te.exercise_id = tex.id
           WHERE ts.date >= ?""",
        (monday_str,),
    )
    kpi_volume = 0
    kpi_reps = 0
    if week_stats:
        row = week_stats[0]
        kpi_volume = round(row["core_volume"] or 0)
        kpi_reps = row["total_reps"] or 0

    # --- Personal Bests (last 12 weeks, Core exercises only) ---
    twelve_weeks_ago = (today - timedelta(weeks=12)).isoformat()
    pb_rows = await db.execute_fetchall(
        """SELECT tex.name, MAX(te.weight) as max_weight
           FROM training_entries te
           JOIN training_sessions ts ON te.session_id = ts.id
           JOIN training_exercises tex ON te.exercise_id = tex.id
           WHERE ts.date >= ? AND tex.section = 'Core' AND te.weight > 0
           GROUP BY tex.id
           ORDER BY tex.display_order""",
        (twelve_weeks_ago,),
    )
    personal_bests = [dict(r) for r in pb_rows]

    # The exercise library is no longer read here either. It is still the
    # parser's vocabulary (canonical_movements) and is still edited in
    # Settings → Exercise Library — this page just has no picker to feed.
    return templates.TemplateResponse(
        "training.html",
        {
            "request": request,
            "sessions": sessions,
            "today": today.isoformat(),
            "kpi_sessions": kpi_sessions,
            "kpi_volume": kpi_volume,
            "kpi_reps": kpi_reps,
            "personal_bests": personal_bests,
        },
    )


@router.post("/training/wod")
async def capture_wod(request: Request):
    """Capture a free-text WOD note, parse it, and redirect to the confirm screen.

    Post/Redirect/Get: the parse result is persisted (training_sessions.wod_parsed)
    so GET /training/wod/confirm/{session_id} can render it without re-invoking the
    LLM. This is what stops a double-submit or an F5 from creating a second session
    and firing a second paid parse call — before this, replaying the POST created
    session #2, the confirm form silently rebound to it, and session #1 survived
    entry-less while still counting toward the weekly kpi_sessions KPI.

    The session row and the user's own words are still committed BEFORE the LLM is
    called, so a parser failure costs structure, never the record.
    """
    db = get_user_db_from_request(request)
    form = await request.form()

    session_date = form.get("date", date.today().isoformat())
    if not valid_date(session_date):
        return RedirectResponse("/training", status_code=303)

    wod_text = truncate(form.get("wod_text", "").strip(), 4000)
    if not wod_text:
        return RedirectResponse("/training", status_code=303)

    duration_int = _parse_int_in_range(form.get("duration_minutes"), 1, int(DURATION_MINUTES_MAX))

    cursor = await db.execute(
        "INSERT INTO training_sessions (date, duration_minutes, notes) VALUES (?, ?, ?)",
        (session_date, duration_int, wod_text),
    )
    session_id = cursor.lastrowid
    await db.commit()

    entries: list = []
    unmatched: list[str] = []
    parse_error = ""
    try:
        parsed = await parse_wod(db, wod_text)
        entries, unmatched = parsed.entries, parsed.unmatched
    except Exception as exc:
        # Broadened from `except ValueError`: parse_wod's own call chain raises
        # more than ValueError — app/services/llm.py has bare asserts (missing
        # content, max_tokens bounds), transport errors that aren't
        # litellm.APIError subclasses, and canonical_movements() below now
        # asserts a vocabulary bound (I5) that can also fire mid-parse. Any of
        # those left this session's wod_parsed NULL forever: the GET confirm
        # page 303s away when wod_parsed is unset, and at the time the only
        # other writer always INSERTed a brand-new session, so there was no way
        # to ever attach entries to this one again. That other writer is gone
        # now (the confirm screen is the sole writer of training_entries),
        # which makes this handler load-bearing rather than belt-and-braces:
        # without it, a parse crash strands the session permanently.
        # The INSERT+commit above already happened, so catching wider here
        # weakens no ordering guarantee.
        parse_error = str(exc)
        logger.warning("WOD parse failed for session %s: %s", session_id, exc)

    wod_parsed = json.dumps(
        {
            "entries": [asdict(e) for e in entries],
            "unmatched": unmatched,
            "parse_error": parse_error,
        }
    )
    await db.execute("UPDATE training_sessions SET wod_parsed = ? WHERE id = ?", (wod_parsed, session_id))
    await db.commit()

    return RedirectResponse(f"/training/wod/confirm/{session_id}", status_code=303)


@router.get("/training/wod/confirm/{session_id}", response_class=HTMLResponse)
async def wod_confirm_page(request: Request, session_id: int):
    """Render the WOD confirmation screen from the STORED parse result.

    Never re-parses: a GET (including a plain browser refresh) must never
    invoke the LLM again — that's the whole point of persisting the result in
    capture_wod rather than rendering it directly there.
    """
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall("SELECT id, date, wod_parsed FROM training_sessions WHERE id = ?", (session_id,))
    if not rows or not rows[0]["wod_parsed"]:
        # Unknown session, or one not created by the WOD capture flow (no
        # stored parse result to show) — nothing to confirm here.
        return RedirectResponse("/training", status_code=303)

    session = rows[0]
    try:
        parsed = json.loads(session["wod_parsed"])
    except (json.JSONDecodeError, TypeError, ValueError):
        # A corrupt stored value must not 500 this GET forever (M3, rides
        # along with I3) — fall back to the same "nothing to confirm" redirect
        # used above when there is no stored result at all.
        logger.warning("Corrupt wod_parsed for session %s — nothing to confirm", session_id)
        return RedirectResponse("/training", status_code=303)

    try:
        movements = await canonical_movements(db)
        library_error = ""
    except AssertionError as exc:
        # I5 bounds the CrossFit vocabulary with an assert in canonical_movements().
        # POST /api/library is MCP-callable, so the library can grow past that
        # bound between capture (where I3's broadened `except Exception` already
        # absorbs this) and the user opening this GET — which had no guard at
        # all. Left unguarded, that reopens exactly the failure class I3 was
        # written to eliminate: a permanent 500 on a session whose note and
        # wod_parsed are already safely stored. Degrade instead — empty picker,
        # error surfaced, same shape as the M3 guard on json.loads above.
        logger.warning("WOD confirm movements list unavailable for session %s: %s", session_id, exc)
        movements = []
        library_error = str(exc)

    return templates.TemplateResponse(
        "wod_confirm.html",
        {
            "request": request,
            "session_id": session_id,
            "session_date": session["date"],
            "entries": parsed.get("entries", []),
            "unmatched": parsed.get("unmatched", []),
            "parse_error": parsed.get("parse_error", ""),
            "movements": movements,
            "library_error": library_error,
        },
    )


@router.post("/training/wod/confirm")
async def confirm_wod(request: Request):
    """Persist the user-reviewed WOD entries against an existing session.

    Two safety properties, both from the 2026-07-30 review:

    B1 — replay safety. The write only proceeds if it can atomically flip
    `wod_parsed` from "set" to NULL for this session (`rowcount == 1`). A
    replayed POST (double submit, or Back-then-resubmit on the now-permanent
    GET /training/wod/confirm/{id} URL) finds `wod_parsed` already NULL and
    is redirected without writing anything — never a second set of entries.
    This makes the write at least as guarded as the GET, which already
    requires `wod_parsed IS NOT NULL` to render anything.

    B2 — no silent discard. Every field is parsed with `_confirm_int`/
    `_confirm_float`, which raise `_ConfirmRejected` for a value that is
    present but out of range (never silently None/clamped-to-1). Validation
    runs BEFORE the B1 consume step, so a rejected submission leaves
    `wod_parsed` untouched — the confirm screen is still there to retry
    against, not consumed by the very request that failed to write anything.
    """
    db = get_user_db_from_request(request)
    form = await request.form()

    session_id = _parse_int_in_range(form.get("session_id"), 1, 2**31 - 1)
    if session_id is None:
        return RedirectResponse("/training", status_code=303)

    entry_count_raw = form.get("entry_count")
    entry_count = _parse_int_in_range(entry_count_raw, 0, 200)
    if entry_count is None:
        logger.warning(
            "WOD confirm rejected for session %s: entry_count=%r out of [0, 200]", session_id, entry_count_raw
        )
        return RedirectResponse(
            f"/training/wod/confirm/{session_id}?err={quote('Zbyt dużo wpisów naraz — spróbuj ponownie.')}",
            status_code=303,
        )

    parsed_rows: list[tuple[str, int, int | None, float | None, float | None, str]] = []
    try:
        for i in range(entry_count):
            movement = (form.get(f"entry_{i}_movement") or "").strip()
            set_number = _confirm_int(form.get(f"entry_{i}_set_number"), 1, 100, "seria", i)
            reps = _confirm_int(form.get(f"entry_{i}_reps"), 0, REPS_MAX, "powtórzenia", i)
            weight = _confirm_float(form.get(f"entry_{i}_weight"), 0, WEIGHT_KG_MAX, "ciężar", i)
            duration = _confirm_float(form.get(f"entry_{i}_duration"), 0, DURATION_SECONDS_MAX, "czas", i)
            note = truncate(form.get(f"entry_{i}_note", ""), 200)
            parsed_rows.append((movement, set_number if set_number is not None else 1, reps, weight, duration, note))
    except _ConfirmRejected as exc:
        logger.warning("WOD confirm rejected for session %s: %s", session_id, exc)
        return RedirectResponse(f"/training/wod/confirm/{session_id}?err={quote(str(exc))}", status_code=303)

    # B1: atomically consume the pending parse result. rowcount != 1 means
    # "unknown session" or "already confirmed" (replay) — either way, redirect
    # without writing rather than risk a second set of entries.
    cursor = await db.execute(
        "UPDATE training_sessions SET wod_parsed = NULL WHERE id = ? AND wod_parsed IS NOT NULL",
        (session_id,),
    )
    if cursor.rowcount != 1:
        await db.commit()
        return RedirectResponse("/training", status_code=303)

    rows: list[tuple] = []
    for movement, set_number, reps, weight, duration, note in parsed_rows:
        exercise_id = await resolve_movement(db, movement)
        if exercise_id is None:
            # Blank movement ("— pomiń", I4) or one that no longer resolves —
            # not an error, just nothing to write for this row.
            continue
        rows.append((session_id, exercise_id, set_number, reps, weight, duration, note))

    if rows:
        await db.executemany(
            "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    await db.commit()
    return RedirectResponse("/training", status_code=303)


@router.post("/training/session/{session_id}/delete")
async def delete_session(request: Request, session_id: int):
    db = get_user_db_from_request(request)
    await db.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    await db.commit()
    return RedirectResponse("/training", status_code=303)
