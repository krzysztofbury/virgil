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

# Rows one confirm submission may carry. Exceeding it rejects the WHOLE
# submission (see the entry_count check in confirm_wod), so the confirm template
# is given this number and stops adding rows at it, rather than letting the user
# build a form that cannot be saved and losing the lot on submit.
MAX_CONFIRM_ENTRIES = 200

# Blank rows offered when the parser produced nothing to review.
#
# One row was not a manual-entry path. It holds a single set of a single
# movement, and a WOD is never that: the note that motivated this carried a
# warm-up, six snatch singles and a three-movement AMRAP. The add-row buttons
# are Alpine, which loads deferred from a CDN with no vendored fallback, so on
# the exact screen a parse failure lands on, these server-rendered rows are the
# only manual entry that survives that script not arriving.
#
# Blank rows cost nothing on submit: confirm_wod resolves an empty movement to
# None and skips the row, the same way it treats the skip option.
SEED_ROWS_ON_PARSE_FAILURE = 5


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
    parsed = None
    unusable = ""
    try:
        parsed = json.loads(session["wod_parsed"])
    except (json.JSONDecodeError, TypeError, ValueError):
        # A corrupt stored value must not 500 this GET forever (M3, rides
        # along with I3).
        unusable = "corrupt JSON"
    else:
        if not isinstance(parsed, dict):
            # M3 covered "json.loads raised" but not "json.loads succeeded and
            # returned something that is not a mapping". `null`, `[]`, `123` and
            # `"x"` are all valid JSON and every one of them reached
            # parsed.get(...) below as an AttributeError — a permanent 500 on the
            # very GET that guard exists to keep reachable.
            unusable = f"{type(parsed).__name__}, not an object"

    if unusable:
        # Clear it rather than redirecting and leaving it armed. An unreadable
        # parse carries no information, so nothing is lost — but leaving it in
        # place kept the session flagged "dokończ" on /training forever, behind a
        # link that only redirects back here. The user's only escape was deleting
        # the session, which cascades and takes the raw note with it: exactly what
        # this screen's own prose tells them not to do.
        #
        # A write on a GET, deliberately: it is idempotent, repairs local
        # corruption, and touches only a value that could never be read.
        logger.warning("wod_parsed for session %s is unusable (%s) — clearing it", session_id, unusable)
        await db.execute("UPDATE training_sessions SET wod_parsed = NULL WHERE id = ?", (session_id,))
        await db.commit()
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
            "seed_rows": SEED_ROWS_ON_PARSE_FAILURE,
            "max_entries": MAX_CONFIRM_ENTRIES,
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

    # "Discard this parse": the supported way to end up with the note and no
    # entries. Without an explicit exit, "I reviewed this and want none of it" is
    # byte-identical to "nothing resolved", and one of the two has to be refused.
    #
    # Hoisted above every field check on purpose. It reads no entry_* field and
    # no entry_count, so validating them first could only ever refuse it — and it
    # did: the parser applies no upper bound to reps/weight/duration, so an LLM
    # returning `reps: 5000` for "row 5000m" put a value past REPS_MAX into
    # wod_parsed, which the GET rendered straight back into the form. Every
    # submission then bounced off _confirm_int, discard included. The worse the
    # parse, the more surely the escape hatch was unavailable. `formnovalidate`
    # on the button does not help — it suppresses the browser's check, not this
    # one.
    #
    # Conditioned the same way the consume below is. "Nothing to lose here" was
    # wrong for any id other than the one on screen: a stale tab or a bfcache
    # resubmission carrying an old session_id would discard a parse the user
    # never looked at, silently and with a success redirect. Reaching another
    # user's row is already impossible — app/auth.py opens the caller's own
    # database file — but same-user collateral damage was not.
    if form.get("action") == "discard":
        cursor = await db.execute(
            "UPDATE training_sessions SET wod_parsed = NULL WHERE id = ? AND wod_parsed IS NOT NULL",
            (session_id,),
        )
        if cursor.rowcount == 0:
            logger.warning("WOD discard for session %s matched nothing (unknown id or already settled)", session_id)
        await db.commit()
        return RedirectResponse("/training", status_code=303)

    entry_count_raw = form.get("entry_count")
    entry_count = _parse_int_in_range(entry_count_raw, 0, MAX_CONFIRM_ENTRIES)
    if entry_count is None:
        # Two different causes, two different messages. They used to share one:
        # "too many entries at once — try again", which sent a user whose
        # entry_count was missing or unparseable looking for a row-count problem
        # they did not have, and told them to retry an action that could not
        # succeed. Absent/blank is the interesting case — it means the hidden
        # field never made it into the submission.
        logger.warning(
            "WOD confirm rejected for session %s: entry_count=%r out of [0, 200]", session_id, entry_count_raw
        )
        blank = entry_count_raw is None or str(entry_count_raw).strip() == ""
        message = (
            "Formularz dotarł niekompletny — odśwież stronę i spróbuj ponownie. Notatka jest zapisana."
            if blank
            else "Zbyt dużo wpisów naraz — spróbuj ponownie."
        )
        return RedirectResponse(f"/training/wod/confirm/{session_id}?err={quote(message)}", status_code=303)

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
    #
    # The pending value is read first so it can be put back: see the re-arm
    # below.
    pending_rows = await db.execute_fetchall("SELECT wod_parsed FROM training_sessions WHERE id = ?", (session_id,))
    pending = pending_rows[0]["wod_parsed"] if pending_rows else None
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

    if not rows:
        # Nothing resolved, so the consume above took the parse from a session
        # that gained nothing — and the confirm GET 303s away once wod_parsed is
        # NULL, which strands the session with no form and no route back. Put it
        # back and say so.
        #
        # Two earlier attempts guarded this *before* the consume, on whether any
        # row named a movement. That predicate is not the one the write uses:
        # resolve_movement also returns None for a name that resolves to nothing.
        # A client can post any string, and an archived exercise_library row stops
        # resolving once no training_exercises row carries that name — the
        # training_exercises lookup runs first and reactivates archived rows
        # there, so archiving alone only breaks movements never logged before.
        # The guard has to be the postcondition itself — `rows` — not a proxy for
        # it, so this sits after the resolve loop.
        #
        # Re-arming keeps B1 idempotent: atomicity comes from the conditional
        # WHERE above, and a replay reaching this branch also writes nothing.
        #
        # rowcount == 1 above proves wod_parsed was non-NULL one statement
        # earlier, so `pending` must carry it. Asserted rather than assumed: the
        # read is in autocommit (sqlite3 issues BEGIN only before DML), and if it
        # ever came back NULL this UPDATE would strand exactly the session this
        # branch exists to save. The WHERE clause makes it structural — re-arm
        # only what is still consumed.
        assert pending is not None, f"session {session_id}: consume succeeded but no pending parse was read"
        await db.execute(
            "UPDATE training_sessions SET wod_parsed = ? WHERE id = ? AND wod_parsed IS NULL",
            (pending, session_id),
        )
        await db.commit()
        logger.warning("WOD confirm resolved no movements for session %s; parse re-armed", session_id)
        return RedirectResponse(
            f"/training/wod/confirm/{session_id}?err="
            + quote(
                "Żaden wiersz nie wskazał znanego ruchu, więc nic nie zapisano. "
                "Wybierz ruch z listy albo użyj „Odrzuć parsowanie”, jeśli notatka wystarczy."
            ),
            status_code=303,
        )

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
