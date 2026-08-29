import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import date, timedelta
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.feedback import error_redirect, success_redirect
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

# Skipped movement names listed in the message. A bounded list keeps one bad
# parse from building a redirect URL nothing will accept.
MAX_SKIPPED_NAMED = 5

# History is paginated rather than capped. A capped list silently hid a
# backdated session and every route to it, including the "dokończ" link the
# confirm screen promises is there.
SESSIONS_PER_PAGE = 20

# Both remaining lists are bounded too: MAX_HISTORY_PAGES bounds the OFFSET a
# hand-typed page number can ask for, and MAX_PENDING_LISTED bounds the pending
# card. A pending list longer than that means something upstream is wrong, so the
# page says so instead of growing without limit.
MAX_HISTORY_PAGES = 500
MAX_PENDING_LISTED = 50


# The picker puts recently logged movements on top. A flat library listing makes
# the user scan 70 rows to find the movement they did last week.
RECENT_MOVEMENT_DAYS = 60
RECENT_MOVEMENT_LIMIT = 8


async def movement_tags(db) -> dict[str, list[str]]:
    """Library name -> its tags, for the picker only.

    Deliberately not folded into canonical_movements(): that function builds the
    parser's prompt vocabulary, it runs on every parse, and its test fixture
    creates exercise_library without the tag table. A picker concern does not
    belong on that path.
    """
    rows = await db.execute_fetchall(
        "SELECT l.name, t.tag FROM exercise_library l "
        "JOIN exercise_library_tags t ON t.library_id = l.id "
        "WHERE l.archived = 0"
    )
    tags: dict[str, list[str]] = {}
    for r in rows:
        tags.setdefault(r["name"], []).append(r["tag"])
    for names in tags.values():
        names.sort()
    return tags


async def recent_movements(db, limit: int = RECENT_MOVEMENT_LIMIT) -> list[str]:
    """Movement names the user logged most recently, newest first."""
    assert limit > 0, f"limit must be positive, got {limit}"
    since = (date.today() - timedelta(days=RECENT_MOVEMENT_DAYS)).isoformat()
    rows = await db.execute_fetchall(
        "SELECT tex.name AS name, MAX(ts.date) AS last_date "
        "FROM training_entries te "
        "JOIN training_sessions ts ON te.session_id = ts.id "
        "JOIN training_exercises tex ON te.exercise_id = tex.id "
        "WHERE ts.date >= ? GROUP BY tex.name ORDER BY last_date DESC LIMIT ?",
        (since, limit),
    )
    return [r["name"] for r in rows]


def _as_input(value) -> str:
    """A stored value as an HTML input value: None becomes blank, never "None"."""
    return "" if value is None else str(value)


def _blank_row(index: int, unmatched_label: str = "") -> dict:
    return {
        "index": index,
        "movement": "",
        "set_number": "1",
        "reps": "",
        "weight": "",
        "duration": "",
        "note": "",
        "unmatched_label": unmatched_label,
    }


def _confirm_rows(parsed: dict, seed_rows: int) -> list[dict]:
    """The confirm screen's rows, in one list, numbered once.

    The template used to build parsed rows, unmatched rows, seed rows and
    Alpine rows from four copies of the same markup, each with its own index
    arithmetic. One list with one index per row removes that duplication, and
    it gives confirm_wod something to re-render a rejected submission from.

    seed_rows is 0 when there is no vocabulary to pick from: a select whose only
    option is "pomiń" is a manual-entry path that cannot write anything.
    """
    rows: list[dict] = []
    for entry in parsed.get("entries") or []:
        if not isinstance(entry, dict):
            # Same reasoning as the parser's own guard: a non-object carries no
            # movement name, so there is nothing to render or resolve.
            logger.warning("confirm rows skipped a non-object entry: %r", entry)
            continue
        rows.append(
            {
                "index": len(rows),
                "movement": _as_input(entry.get("movement")),
                "set_number": _as_input(entry.get("set_number")) or "1",
                "reps": _as_input(entry.get("reps")),
                "weight": _as_input(entry.get("weight")),
                "duration": _as_input(entry.get("duration")),
                "note": _as_input(entry.get("note")),
                "unmatched_label": "",
            }
        )
    for name in parsed.get("unmatched") or []:
        rows.append(_blank_row(len(rows), unmatched_label=str(name)))
    if not rows:
        rows = [_blank_row(i) for i in range(seed_rows)]
    return rows


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
async def training_page(request: Request, page: int = 1):
    db = get_user_db_from_request(request)

    # Sessions with a pending parse come first, and independently of the history
    # window: a backdated capture used to fall off the newest-20 list together
    # with the one link that leads back to its confirm screen.
    pending_rows = await db.execute_fetchall(
        "SELECT id, date, notes FROM training_sessions WHERE wod_parsed IS NOT NULL ORDER BY date DESC LIMIT ?",
        (MAX_PENDING_LISTED + 1,),
    )
    pending_sessions = [dict(p) for p in pending_rows]
    pending_overflow = len(pending_sessions) > MAX_PENDING_LISTED
    pending_sessions = pending_sessions[:MAX_PENDING_LISTED]

    # training_exercises is no longer read here: the page has no protocol list
    # and no per-exercise log form. The table itself stays — training_entries
    # references it, so every logged set (past and future) hangs off it, and the
    # WOD parser keeps creating rows there via resolve_movement().
    #
    # One row past the page size, so "is there a next page" needs no COUNT(*).
    page = min(max(page, 1), MAX_HISTORY_PAGES)
    sessions = await db.execute_fetchall(
        "SELECT * FROM training_sessions ORDER BY date DESC, id DESC LIMIT ? OFFSET ?",
        (SESSIONS_PER_PAGE + 1, (page - 1) * SESSIONS_PER_PAGE),
    )
    sessions = [dict(s) for s in sessions]
    has_next = len(sessions) > SESSIONS_PER_PAGE
    sessions = sessions[:SESSIONS_PER_PAGE]

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

    for s in sessions:
        # A session holding only the raw note: the parse never landed (a crash
        # between capture_wod's two commits) or the user discarded it. Offer
        # manual entry rather than leaving the note as the only record.
        s["stranded"] = bool(s["notes"]) and not s["entries"] and not s["wod_parsed"]

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
            # One token per rendered page. capture_wod uses it to tell a replay
            # of THIS page's submission from a genuine second capture.
            "capture_token": uuid4().hex,
            "pending_sessions": pending_sessions,
            "pending_overflow": pending_overflow,
            "page": page,
            "has_prev": page > 1,
            "has_next": has_next,
        },
    )


async def _resolve_capture_replay(db, capture_token, session_date, wod_text, duration_int):
    """Decide what a refused capture_token means, and act on it.

    Returns `(replay_id, session_id)`. Exactly one is set: `replay_id` for a
    true replay, whose parse already exists, and `session_id` for a fresh row
    that still needs one. `(None, None)` means the claim failed for a reason
    this function must not guess at.

    The token is minted by GET /training, so ONE rendered page can submit it
    more than once. A double click carries the same token AND the same note:
    that is a replay, and the first session is the right destination. The back
    button is different. It restores the same page from the bfcache with the
    same token, and the user then types a DIFFERENT note. Treating that as a
    replay would redirect them to another session's confirm screen and drop the
    note they just wrote, silently. So the payload decides, not the token: same
    date and same text means replay, anything else is a new capture that gets
    its own row with no token to collide with.
    """
    owner = await db.execute_fetchall(
        "SELECT id, date, notes FROM training_sessions WHERE capture_token = ?", (capture_token,)
    )
    if not owner:
        # The unique index refused the row but no session owns the token, so the
        # cause is something else entirely. Do not write a second session on a
        # guess.
        logger.warning("capture claim for token %r failed and matched no session", capture_token)
        return None, None

    row = owner[0]
    if row["date"] == session_date and (row["notes"] or "") == wod_text:
        logger.info("capture replay for token %r -> session %s", capture_token, row["id"])
        return row["id"], None

    logger.info("capture token %r reused with a new note - writing a fresh session", capture_token)
    cursor = await db.execute(
        "INSERT INTO training_sessions (date, duration_minutes, notes, capture_token) VALUES (?, ?, ?, NULL)",
        (session_date, duration_int, wod_text),
    )
    await db.commit()
    return None, cursor.lastrowid


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
        return error_redirect(request, "/training", "Nieprawidłowa data treningu.")

    wod_text = truncate(form.get("wod_text", "").strip(), 4000)
    if not wod_text:
        return error_redirect(request, "/training", "Wpisz notatkę treningową.")

    duration_int = _parse_int_in_range(form.get("duration_minutes"), 1, int(DURATION_MINUTES_MAX))

    # One capture per click. The token is minted by the form GET /training
    # rendered, so a double submit, an F5 or a bfcache replay all carry the same
    # value and the unique index refuses the second row. That refusal is a
    # decision, not an error: see _resolve_capture_replay.
    capture_token = truncate(form.get("capture_token", "").strip(), 64) or None
    try:
        cursor = await db.execute(
            "INSERT INTO training_sessions (date, duration_minutes, notes, capture_token) VALUES (?, ?, ?, ?)",
            (session_date, duration_int, wod_text, capture_token),
        )
        session_id = cursor.lastrowid
        await db.commit()
    except sqlite3.IntegrityError:
        await db.rollback()
        replay_id, session_id = await _resolve_capture_replay(db, capture_token, session_date, wod_text, duration_int)
        if replay_id is not None:
            # Same token, same note: the first request already stored the parse.
            return success_redirect(
                request,
                f"/training/wod/confirm/{replay_id}",
                "Notatka treningowa zapisana. Sprawdź wpisy przed zatwierdzeniem.",
            )
        if session_id is None:
            return error_redirect(request, "/training", "Nie udało się zapisać notatki treningowej.")

    entries: list = []
    unmatched: list[str] = []
    dropped = 0
    parse_error = ""
    try:
        parsed = await parse_wod(db, wod_text)
        entries, unmatched, dropped = parsed.entries, parsed.unmatched, parsed.dropped
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
            "dropped": dropped,
        }
    )
    await db.execute("UPDATE training_sessions SET wod_parsed = ? WHERE id = ?", (wod_parsed, session_id))
    await db.commit()

    return success_redirect(
        request,
        f"/training/wod/confirm/{session_id}",
        "Notatka treningowa zapisana. Sprawdź wpisy przed zatwierdzeniem.",
    )


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

    return await _render_confirm(
        request,
        session_id,
        session["date"],
        parsed=parsed,
        movements=movements,
        library_error=library_error,
        error=request.query_params.get("err", ""),
    )


async def _render_confirm(
    request: Request,
    session_id: int,
    session_date: str,
    *,
    parsed: dict | None = None,
    rows: list[dict] | None = None,
    movements: list[dict] | None = None,
    library_error: str = "",
    error: str = "",
) -> HTMLResponse:
    """Render the confirm screen. One renderer for the GET and for a refusal.

    Pass `parsed` for the GET and `rows` for a refusal, never both. A refusal
    renders the SUBMITTED rows: answering it with a redirect to this page's own
    GET rebuilt the form from the stored parse and threw away every edit the
    user had made, rows they had added included.

    A refusal therefore answers a POST with 200, which drops Post/Redirect/Get
    on that path alone. That is safe here: a refusal writes nothing and leaves
    wod_parsed armed, so a refresh re-posts the same values and earns the same
    refusal. The success path keeps its 303.
    """
    assert (parsed is None) != (rows is None), "pass parsed for the GET or rows for a refusal, never both"
    db = get_user_db_from_request(request)

    if movements is None:
        try:
            movements = await canonical_movements(db)
            library_error = ""
        except AssertionError as exc:
            logger.warning("WOD confirm movements list unavailable for session %s: %s", session_id, exc)
            movements = []
            library_error = str(exc)

    parsed = parsed or {}
    if rows is None:
        # No vocabulary means no seed rows: a picker whose only option is
        # "pomiń" is a manual-entry path that cannot write anything.
        rows = _confirm_rows(parsed, SEED_ROWS_ON_PARSE_FAILURE if movements else 0)
        parsed_row_count = len(parsed.get("entries") or []) + len(parsed.get("unmatched") or [])
    else:
        # A re-render always has rows on screen, so the "nothing parsed" prose
        # below is not the message this screen needs to carry.
        parsed_row_count = len(rows)

    return templates.TemplateResponse(
        "wod_confirm.html",
        {
            "request": request,
            "session_id": session_id,
            "session_date": session_date,
            "rows": rows,
            "parsed_row_count": parsed_row_count,
            "parse_error": parsed.get("parse_error", ""),
            "dropped": parsed.get("dropped", 0),
            "movements": movements,
            "tags": await movement_tags(db),
            "recent": await recent_movements(db),
            "library_error": library_error,
            "seed_rows": SEED_ROWS_ON_PARSE_FAILURE,
            "max_entries": MAX_CONFIRM_ENTRIES,
            "error": error,
        },
    )


def _rows_from_form(form, entry_count: int) -> list[dict]:
    """The submitted rows, in the shape the template renders.

    Raw strings on purpose: the rejected value must come back on screen exactly
    as the user typed it, so they can see what the message is about.
    """
    rows: list[dict] = []
    for i in range(entry_count):
        rows.append(
            {
                "index": i,
                "movement": (form.get(f"entry_{i}_movement") or "").strip(),
                "set_number": str(form.get(f"entry_{i}_set_number") or "1"),
                "reps": str(form.get(f"entry_{i}_reps") or ""),
                "weight": str(form.get(f"entry_{i}_weight") or ""),
                "duration": str(form.get(f"entry_{i}_duration") or ""),
                "note": truncate(form.get(f"entry_{i}_note", ""), 200),
                "unmatched_label": "",
            }
        )
    return rows


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
        return error_redirect(request, "/training", "Nieprawidłowy identyfikator sesji treningowej.")

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
            return error_redirect(
                request, "/training", "Nie odrzucono parsowania: sesja jest nieznana lub nieaktualna."
            )
        await db.commit()
        return success_redirect(request, "/training", "Parsowanie odrzucone. Notatka treningowa została zachowana.")

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
        return error_redirect(request, f"/training/wod/confirm/{session_id}", message)

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
        date_rows = await db.execute_fetchall("SELECT date FROM training_sessions WHERE id = ?", (session_id,))
        session_date = date_rows[0]["date"] if date_rows else date.today().isoformat()
        return await _render_confirm(
            request,
            session_id,
            session_date,
            rows=_rows_from_form(form, entry_count),
            error=f"{exc}. Reszta formularza jest zachowana - popraw to jedno pole i zapisz ponownie.",
        )

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
        return error_redirect(request, "/training", "Nie zapisano wpisów: sesja jest nieznana lub nieaktualna.")

    rows: list[tuple] = []
    skipped: list[str] = []
    for movement, set_number, reps, weight, duration, note in parsed_rows:
        exercise_id = await resolve_movement(db, movement)
        if exercise_id is None:
            # A blank movement is the deliberate skip ("- pomiń", I4) and needs
            # no report. A NAMED movement that does not resolve is a different
            # thing: the user reviewed that row and it vanished anyway, with no
            # message, because the guard below only fires when NOTHING resolved.
            if movement:
                skipped.append(movement)
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
        return error_redirect(
            request,
            f"/training/wod/confirm/{session_id}",
            "Żaden wiersz nie wskazał znanego ruchu, więc nic nie zapisano. "
            "Wybierz ruch z listy albo użyj „Odrzuć parsowanie”, jeśli notatka wystarczy.",
        )

    await db.executemany(
        "INSERT INTO training_entries (session_id, exercise_id, set_number, reps, weight, duration, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()

    if skipped:
        names = ", ".join(sorted(set(skipped))[:MAX_SKIPPED_NAMED])
        message = (
            f"Zapisano {len(rows)} z {len(rows) + len(skipped)} wierszy. "
            f"Pominięto ruchy, których nie ma w bibliotece: {names}. "
            "Dodaj je w Ustawieniach i dopisz kolejną notatką."
        )
        logger.warning("WOD confirm skipped unresolved movements for session %s: %s", session_id, names)
        return success_redirect(request, "/training", message)
    return success_redirect(request, "/training", f"Zapisano {len(rows)} wpisów treningowych.")


@router.post("/training/session/{session_id}/manual")
async def arm_manual_entry(request: Request, session_id: int):
    """Arm an empty parse so a stranded session can gain entries.

    The confirm GET needs a non-NULL wod_parsed to render anything, so an empty
    one gives the user the same manual seed rows a failed parse already gets.

    Refused for a session that has entries or a pending parse: re-arming either
    offers a second write against a session that is already settled.
    """
    db = get_user_db_from_request(request)
    rows = await db.execute_fetchall(
        "SELECT s.wod_parsed AS wod_parsed, COUNT(e.id) AS entries "
        "FROM training_sessions s LEFT JOIN training_entries e ON e.session_id = s.id "
        "WHERE s.id = ? GROUP BY s.id",
        (session_id,),
    )
    if not rows or rows[0]["entries"] or rows[0]["wod_parsed"] is not None:
        logger.warning("manual entry refused for session %s (unknown, has entries, or already armed)", session_id)
        return error_redirect(request, "/training", "Nie można otworzyć ręcznego wpisu dla tej sesji.")

    armed = json.dumps({"entries": [], "unmatched": [], "parse_error": "", "dropped": 0, "manual": True})
    cursor = await db.execute(
        "UPDATE training_sessions SET wod_parsed = ? WHERE id = ? AND wod_parsed IS NULL",
        (armed, session_id),
    )
    await db.commit()
    if cursor.rowcount != 1:
        # Another request armed it between the read and this write.
        logger.warning("manual entry for session %s lost the arming race", session_id)
        return error_redirect(request, "/training", "Sesja zmieniła się przed otwarciem ręcznego wpisu.")
    return success_redirect(
        request,
        f"/training/wod/confirm/{session_id}",
        "Ręczny wpis jest gotowy. Uzupełnij i zapisz ćwiczenia.",
    )


@router.post("/training/session/{session_id}/delete")
async def delete_session(request: Request, session_id: int):
    db = get_user_db_from_request(request)
    cursor = await db.execute("DELETE FROM training_sessions WHERE id = ?", (session_id,))
    await db.commit()
    if cursor.rowcount == 0:
        return error_redirect(request, "/training", "Nie usunięto sesji: sesja nie istnieje.")
    return success_redirect(request, "/training", "Sesja treningowa usunięta.")
