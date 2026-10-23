# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp>=2,<3",
#     "httpx>=0.27",
# ]
# ///
"""Virgil MCP server — thin stdio wrapper over the Virgil REST API.

Exposes Virgil data (daily habits, Oura, streaks, experiments, training, finance)
and its writes (goals, daily log and A.N.D.Y., Freedom habits, training,
finance balances) to MCP
clients (Claude Code, OpenClaw). No local DB access — everything goes
through the authenticated REST API, so it works from any machine.

Config (env):
    VIRGIL_API_URL          - base URL, e.g. https://virgil.example.com (required)
    VIRGIL_API_KEY          - a personal API token from Settings > Security, or the server's VIRGIL_API_KEY (required)
    CF_ACCESS_CLIENT_ID     - Cloudflare Access service token id (if host sits behind CF Access)
    CF_ACCESS_CLIENT_SECRET - Cloudflare Access service token secret

Run:
    uv run --locked --script mcp_server/virgil_mcp.py

Register in Claude Code:
    claude mcp add virgil \
      -e VIRGIL_API_URL=https://virgil.example.com \
      -e VIRGIL_API_KEY=<key> \
      -- uv run --locked --script /path/to/virgil/mcp_server/virgil_mcp.py

The Virgil app also serves these same tools over Streamable HTTP at /mcp
(app/mcp_endpoint.py), so a client can connect without a local checkout.
"""

import os
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API_URL = os.environ.get("VIRGIL_API_URL", "").rstrip("/")
API_KEY = os.environ.get("VIRGIL_API_KEY", "")
CF_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")

mcp = MCPServer("virgil")

_client: httpx.Client | None = None


def use_client(client: httpx.Client | None) -> None:
    """Send every tool call through `client` (base URL and auth headers already set)
    instead of the env config. The hosted /mcp endpoint reaches its own REST API this way."""
    global _client
    _client = client


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=API_URL, headers=_headers(), timeout=30.0)
    return _client


def _headers() -> dict:
    if not API_URL or not API_KEY:
        raise ToolError("Set VIRGIL_API_URL and VIRGIL_API_KEY environment variables")
    headers = {"X-API-Key": API_KEY}
    if CF_ID and CF_SECRET:  # Cloudflare Access gate in front of the app
        headers["CF-Access-Client-Id"] = CF_ID
        headers["CF-Access-Client-Secret"] = CF_SECRET
    return headers


def _check(resp: httpx.Response) -> None:
    """Surface the API's `detail` — a bare '409 Conflict' cannot tell a builtin
    refusal from a name collision, which is the whole point of the API refusing loudly.
    Raised as ToolError because the SDK hides the text of any other exception from the client."""
    if resp.is_error:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        raise ToolError(f"{resp.status_code} {resp.reason_phrase}: {detail or resp.text[:200]}")


def _habit_slug(habit: str) -> str:
    """The habit a Freedom call is about: the given slug, else the first tracked habit."""
    if habit:
        return quote(habit, safe="")
    habits = _get("/api/freedom")["habits"]
    if not habits:
        raise ToolError("No Freedom habit is tracked yet")
    return quote(habits[0]["habit"], safe="")


def _get(path: str, params: dict | None = None) -> dict:
    resp = _http().get(path, params=params)
    _check(resp)
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = _http().post(path, json=payload)
    _check(resp)
    return resp.json()


def _patch(path: str, payload: dict) -> dict:
    resp = _http().patch(path, json=payload)
    _check(resp)
    return resp.json()


def _put(path: str, payload: dict) -> dict:
    resp = _http().put(path, json=payload)
    _check(resp)
    return resp.json()


def _delete(path: str) -> dict:
    resp = _http().delete(path)
    _check(resp)
    return {"deleted": True}


@mcp.tool()
def get_today_summary() -> dict:
    """Today's snapshot: daily habits + energy, the first Freedom habit's streak (key
    "feniks"), latest Oura vitals, training sessions this week, latest body measurements."""
    return _get("/api/summary")


@mcp.tool()
def get_oura_stats() -> dict:
    """Latest synced Oura vitals: sleep/readiness/activity scores, HRV, resting HR, steps."""
    return _get("/api/oura/today")


@mcp.tool()
def get_streaks() -> dict:
    """Freedom status of the first tracked habit (Porn, for an account that started with
    the No Porn module): days since its last slip and that date. get_habits covers every
    habit being tracked. Requires the sensitive scope."""
    status = _get("/api/summary")["feniks"]
    if status is None:
        raise ToolError("Freedom data needs the sensitive scope (a token with it, or VIRGIL_API_SENSITIVE=true)")
    return status


@mcp.tool()
def get_habits() -> dict:
    """Freedom: every habit being broken (porn, alcohol, nicotine, the person's own...) with
    where it stands today: streak, this week's clean rate, today's entry, urges survived
    (bricks), its own definition of a slip (button label, amount unit and threshold, near-miss
    label) and its settings. `habit` in each item is the slug the other Freedom tools take.
    Requires the sensitive scope."""
    return _get("/api/freedom")


@mcp.tool()
def get_weekly_habits(days: int = 7) -> dict:
    """Habit completion logs for the last N days (1-90). Includes energy and
    the 7 daily habits (morning/evening routine, water, body/spirit/self/relations)."""
    return _get("/api/habits", params={"range": days})


@mcp.tool()
def get_experiments() -> dict:
    """Active experiments with current week number, weekly minutes target/progress and
    per-metric progress. Each experiment lists its metrics: kind (duration=minutes,
    count=events, boolean=daily yes/no, scale=0-10 rating), target (target_value per
    target_period: day/week/total) and logged_today / logged_week / logged_total."""
    return _get("/api/experiments/active")


@mcp.tool()
def get_goal_areas() -> dict:
    """Goal-area IDs and labels. Read this before creating a goal when the area ID is unknown."""
    return _get("/api/goal-areas")


@mcp.tool()
def get_goals(status: str = "", focus: bool | None = None, limit: int = 100) -> dict:
    """Canonical goals with lifecycle, date windows, pending-rep counts, and linked experiments.
    `status` is active/paused/completed/abandoned. `focus` filters the starred current-focus set.
    Results are bounded to 1-200 rows."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if focus is not None:
        params["focus"] = focus
    return _get("/api/goals", params=params)


@mcp.tool()
def add_goal(
    area_id: int,
    horizon: str,
    content: str,
    status: str = "active",
    start_date: str = "",
    end_date: str = "",
    focus: bool = False,
    idempotency_key: str = "",
    parent_goal_id: int | None = None,
) -> dict:
    """Create a canonical goal. Horizons are 1yr/3yr/10yr; status is
    active/paused/completed/abandoned. Dates use YYYY-MM-DD. Reuse a non-empty
    idempotency_key when retrying the same write."""
    return _post(
        "/api/goals",
        {
            "area_id": area_id,
            "horizon": horizon,
            "content": content,
            "status": status,
            "start_date": start_date or None,
            "end_date": end_date or None,
            "focus": focus,
            "idempotency_key": idempotency_key,
            "parent_goal_id": parent_goal_id,
        },
    )


@mcp.tool()
def update_goal(
    goal_id: int,
    area_id: int | None = None,
    horizon: str | None = None,
    content: str | None = None,
    status: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    focus: bool | None = None,
    parent_goal_id: int | None = None,
    clear_parent: bool = False,
) -> dict:
    """Update only the supplied goal fields. Pass an empty start_date/end_date to clear that boundary,
    or clear_parent=true to remove the parent link."""
    if clear_parent and parent_goal_id is not None:
        raise ToolError("parent_goal_id and clear_parent cannot be used together")
    payload = {
        key: value
        for key, value in (
            ("area_id", area_id),
            ("horizon", horizon),
            ("content", content),
            ("status", status),
            ("start_date", start_date),
            ("end_date", end_date),
            ("focus", focus),
            ("parent_goal_id", parent_goal_id),
        )
        if value is not None
    }
    if clear_parent:
        payload["parent_goal_id"] = None
    if not payload:
        raise ToolError("update_goal needs at least one field to change")
    return _patch(f"/api/goals/{goal_id}", payload)


@mcp.tool()
def delete_goal(goal_id: int) -> dict:
    """Delete a goal only when it has no execution history. Otherwise mark it abandoned.

    The user can restore a deleted goal from the Goals page for 30 days.
    """
    return _delete(f"/api/goals/{goal_id}")


@mcp.tool()
def get_goal_reps(status: str = "", goal_id: int | None = None, limit: int = 50) -> dict:
    """Bounded execution-rep history. Status is pending/completed/carried/skipped.
    Use status='pending' for the current queue. Weeks always mean Monday through Sunday."""
    params: dict = {"limit": limit}
    if status:
        params["status"] = status
    if goal_id is not None:
        params["goal_id"] = goal_id
    return _get("/api/goal-reps", params=params)


@mcp.tool()
def add_goal_rep(
    goal_id: int,
    content: str,
    period: str,
    due_date: str,
    notes: str = "",
    idempotency_key: str = "",
) -> dict:
    """Add a pending one-off execution rep. Period is day/week/month/quarter/year;
    due_date is YYYY-MM-DD and determines the canonical period bounds. Calendar weeks
    are Monday through Sunday. Reuse idempotency_key when retrying the same write."""
    return _post(
        "/api/goal-reps",
        {
            "goal_id": goal_id,
            "content": content,
            "period": period,
            "due_date": due_date,
            "notes": notes,
            "idempotency_key": idempotency_key,
        },
    )


@mcp.tool()
def update_goal_rep(
    rep_id: int,
    goal_id: int | None = None,
    content: str | None = None,
    period: str | None = None,
    due_date: str | None = None,
    notes: str | None = None,
) -> dict:
    """Edit a pending execution rep. Terminal reps are immutable history."""
    payload = {
        key: value
        for key, value in (
            ("goal_id", goal_id),
            ("content", content),
            ("period", period),
            ("due_date", due_date),
            ("notes", notes),
        )
        if value is not None
    }
    if not payload:
        raise ToolError("update_goal_rep needs at least one field to change")
    return _patch(f"/api/goal-reps/{rep_id}", payload)


@mcp.tool()
def transition_goal_rep(
    rep_id: int,
    action: str,
    due_date: str = "",
    period: str = "",
) -> dict:
    """Complete, skip, or carry a pending rep. Carry requires a new due_date and
    creates a new pending rep while preserving the original as carried history."""
    payload: dict = {"action": action}
    if due_date:
        payload["due_date"] = due_date
    if period:
        payload["period"] = period
    return _post(f"/api/goal-reps/{rep_id}/transition", payload)


@mcp.tool()
def delete_goal_rep(rep_id: int) -> dict:
    """Delete a pending execution rep only when it is outside a carry chain."""
    return _delete(f"/api/goal-reps/{rep_id}")


@mcp.tool()
def log_experiment_entry(experiment_id: int, metric: str, value: int = 1, notes: str = "", date: str = "") -> dict:
    """Log one entry into an active experiment. `metric` is the metric name
    (e.g. 'Gate executed') or its numeric id — see get_experiments. `value` by kind:
    duration=minutes, count=events (default 1), boolean=1 yes / 0 no (one per day,
    last write wins), scale=0-10 rating. `date` YYYY-MM-DD, empty = today."""
    payload: dict = {"metric": metric, "value": value, "notes": notes}
    if date:
        payload["date"] = date
    return _post(f"/api/experiments/{experiment_id}/entries", payload)


@mcp.tool()
def get_training(days: int = 7) -> dict:
    """Training sessions in the last N days (1-90) with entry counts and volume."""
    return _get("/api/training", params={"range": days})


@mcp.tool()
def get_training_detail(days: int = 7) -> dict:
    """Full per-set training detail for the last N days (1-90): every session broken into
    exercises and sets (reps+weight, or weight+seconds for timed lifts like carries/holds).
    Each set carries `notes`, which holds the metcon result the athlete recorded for that
    movement (a finishing time, or rounds plus reps for an AMRAP) - read it before judging
    a session by volume alone.
    Use when the volume summary from get_training isn't enough and you need what was actually done."""
    return _get("/api/training/detail", params={"range": days})


@mcp.tool()
def get_noporn(days: int = 30) -> dict:
    """Freedom's Porn habit (formerly the No Porn module) for the last N days (1-365): config,
    streak, current-week clean rate, relapse/reset events, journal entries (emotions/triggers/thoughts/coping), logged
    pleasures, the daily log (used/minutes/edging/note — the honest metric, a day counter
    cannot see edging) and bricks (urges survived: hook/craving/story — the progress unit).
    Use this to see WHY relapses happened — get_streaks only gives the count."""
    return _get("/api/noporn", params={"range": days})


@mcp.tool()
def get_exercise_library(tag: str = "", include_archived: bool = False) -> dict:
    """The exercise dictionary: the `exercise_library` rows offered when confirming a
    workout, and the vocabulary the parser resolves free-text notes against.
    Archived rows are excluded unless include_archived=True. Each entry's `tags` is a free-form,
    normalised list (lowercased, whitespace-to-dash) — pass tag='crossfit' for
    the WOD vocabulary, or any other tag a movement has been given. Entries
    marked builtin=1 can only be archived or have their tags and classification changed —
    every other field is frozen. Each entry also carries its `family` (e.g. Snatch,
    Squat), `default_equipment` and `aliases` (spellings such as "T2B" or "pistols"
    that resolve to it). The response lists the valid `families` and `equipment`."""
    params: dict = {"include_archived": include_archived}
    if tag:
        params["tag"] = tag
    return _get("/api/library", params=params)


@mcp.tool()
def add_exercise(
    section: str,
    name: str,
    tags: list[str] | None = None,
    metric: str = "reps",
    sets: int | None = None,
    reps: str = "",
    notes: str = "",
    family: str | None = None,
    default_equipment: str | None = None,
    aliases: list[str] | None = None,
) -> dict:
    """Add a movement to the exercise dictionary. `family` groups variants (see
    get_exercise_library for the list), `default_equipment` is the usual equipment,
    `aliases` are other spellings that should resolve to this movement. `section` is Warmup/Core/Cardio/
    Stretching — Core+reps feeds weekly volume; PBs include weighted Core entries
    regardless of metric. Cardio always logs rounds+duration and does not feed volume.
    For Core entries, `metric` 'reps' logs weight+reps (feeds volume/aggregate), while
    'time' logs weight+seconds (excluded from rep aggregates). `tags` are free-form
    labels normalised on write (lowercased, transliterated to ASCII so accented
    letters fold to their base letter, whitespace-to-dash, any other punctuation
    dropped rather than dashed, deduplicated) — tagging a movement 'crossfit' also
    teaches the WOD parser what movements it can recognise. `name` is unique across
    the whole library (not just within a tag or section): a name that already
    exists, in any case, returns 409."""
    return _post(
        "/api/library",
        {
            "section": section,
            "name": name,
            "tags": tags or [],
            "metric": metric,
            "sets": sets,
            "reps": reps,
            "notes": notes,
            "family": family,
            "default_equipment": default_equipment,
            "aliases": aliases or [],
        },
    )


@mcp.tool()
def update_exercise(
    entry_id: int,
    name: str | None = None,
    section: str | None = None,
    metric: str | None = None,
    reps: str | None = None,
    notes: str | None = None,
    sets: int | None = None,
    archived: int | None = None,
    tags: list[str] | None = None,
    family: str | None = None,
    default_equipment: str | None = None,
    aliases: list[str] | None = None,
) -> dict:
    """Edit one dictionary entry — see get_exercise_library for ids. `family`,
    `default_equipment` and `aliases` classify a movement and stay editable on builtin
    entries; an empty string clears family or equipment, and `aliases` replaces the
    whole alias set (an empty list clears it). Only the fields
    you pass change; `tags`, when passed, REPLACES the entry's whole tag set (not a
    merge) and is normalised the same way add_exercise's are. A builtin entry accepts
    tag changes but refuses everything else (name/section/metric/reps/notes/sets) with
    a 409 — `archived` and `tags` are the only fields a builtin row lets through,
    together or separately, and only when the call carries nothing outside those two:
    a call that mixes `tags` and/or `archived` with any of the frozen fields on a
    builtin entry still 409s, and neither `tags` nor `archived` land either — the
    whole call is rejected together, never partially applied. Change a builtin entry's
    `archived`/`tags` in their own call, separate from any of the frozen fields.
    Changing `section`/`metric` on a movement tagged 'crossfit' narrows what the WOD
    parser is allowed to recognise. Renaming (`name`) does NOT narrow that vocabulary —
    the parser just reads whatever name is current — but is refused with a 409 if any
    training history already exists under the old name (archive the entry and add a
    new one instead of renaming through history). Since names are unique library-wide,
    renaming onto an existing name also returns 409."""
    payload = {
        k: v
        for k, v in (
            ("name", name),
            ("section", section),
            ("metric", metric),
            ("reps", reps),
            ("notes", notes),
            ("sets", sets),
            ("archived", archived),
            ("tags", tags),
            ("aliases", aliases),
        )
        if v is not None
    }
    for key, value in (("family", family), ("default_equipment", default_equipment)):
        if value is not None:
            payload[key] = value or None
    if not payload:
        raise ToolError("update_exercise needs at least one field to change")
    return _patch(f"/api/library/{entry_id}", payload)


@mcp.tool()
def delete_exercise(entry_id: int) -> dict:
    """Remove a dictionary entry permanently. Builtin entries refuse deletion —
    archive them instead. Deleting a movement tagged 'crossfit' narrows what the WOD
    parser is allowed to recognise. Deleting does NOT touch logged training history."""
    return _delete(f"/api/library/{entry_id}")


@mcp.tool()
def log_daily(
    date: str,
    energy: int | None = None,
    morning_routine: str | None = None,
    evening_routine: str | None = None,
    water: str | None = None,
    andy_body_status: str | None = None,
    andy_body_desc: str | None = None,
    andy_spirit_status: str | None = None,
    andy_spirit_desc: str | None = None,
    andy_account_status: str | None = None,
    andy_account_desc: str | None = None,
    andy_relations_status: str | None = None,
    andy_relations_desc: str | None = None,
    notes: str | None = None,
    clear_energy: bool = False,
) -> dict:
    """Write one day of the daily log (YYYY-MM-DD): energy 1-10, the routines and the four
    A.N.D.Y. tasks (body/spirit/account/relations), each a done/skipped/pending status plus
    a short description. Only the supplied fields change; the rest of the day keeps its
    stored value, so planning today's A.N.D.Y. and later ticking it off are separate calls.
    clear_energy=true removes a recorded energy value."""
    if clear_energy and energy is not None:
        raise ToolError("energy and clear_energy cannot be used together")
    fields = {
        "energy": energy,
        "morning_routine": morning_routine,
        "evening_routine": evening_routine,
        "water": water,
        "andy_body_status": andy_body_status,
        "andy_body_desc": andy_body_desc,
        "andy_spirit_status": andy_spirit_status,
        "andy_spirit_desc": andy_spirit_desc,
        "andy_account_status": andy_account_status,
        "andy_account_desc": andy_account_desc,
        "andy_relations_status": andy_relations_status,
        "andy_relations_desc": andy_relations_desc,
        "notes": notes,
    }
    payload = {key: value for key, value in fields.items() if value is not None}
    if clear_energy:
        payload["energy"] = None
    if not payload:
        raise ToolError("log_daily needs at least one field to change")
    return _put(f"/api/daily/{date}", payload)


@mcp.tool()
def log_freedom_day(
    habit: str, date: str, slipped: bool, amount: int | None = None, near_miss: bool = False, note: str = ""
) -> dict:
    """Record one Freedom habit's day (YYYY-MM-DD, today or earlier), replacing that day's entry.
    `habit` is a slug from get_habits. slipped=true means the person slipped. `amount` is in
    the habit's own unit (minutes for porn, drinks for alcohol), required for a slip when the
    habit has a threshold (porn: at least 5 minutes; less is slipped=false with the detail in
    `note`). `near_miss` is the habit's near miss (edging for porn), when it has one.
    A slip also marks a relapse for that habit's streak; correcting it to clean removes only
    that marker. Requires the sensitive scope."""
    return _put(
        f"/api/freedom/{_habit_slug(habit)}/days/{date}",
        {"slipped": slipped, "amount": amount, "near_miss": near_miss, "note": note},
    )


@mcp.tool()
def log_feniks_day(
    date: str, used: bool, minutes: int | None = None, edging: bool = False, note: str = "", habit: str = ""
) -> dict:
    """Deprecated: use log_freedom_day. Same write with the No Porn names: used=slipped,
    minutes=amount, edging=near_miss; empty `habit` means the first tracked habit."""
    return log_freedom_day(habit, date, used, minutes, edging, note)


@mcp.tool()
def add_brick(date: str, hook: str, craving: int | None = None, story: str = "", habit: str = "") -> dict:
    """Add a Freedom brick: one urge survived for one habit (`habit` slug from get_habits;
    empty means the first tracked habit). `hook` is the short memorable name (required),
    `craving` 0-10, `story` what happened. Date YYYY-MM-DD, today or earlier.
    Requires the sensitive scope."""
    return _post(
        f"/api/freedom/{_habit_slug(habit)}/bricks", {"date": date, "hook": hook, "craving": craving, "story": story}
    )


@mcp.tool()
def log_training(
    date: str,
    entries: list[dict] | None = None,
    duration_minutes: int | None = None,
    notes: str = "",
    idempotency_key: str = "",
) -> dict:
    """Record a training session with its sets in one write. Each entry is
    {movement, set_number=1, reps, weight (kg), duration (seconds), equipment, note};
    `movement` is an exercise-library name or one of its aliases (see
    get_exercise_library). Set `equipment` only when it differs from the movement's
    default_equipment, e.g. a dumbbell Hang Snatch. An unknown movement refuses the
    whole session and lists the names - add them with add_exercise, then retry.
    `notes` keeps the athlete's own words, e.g. the WOD as written. Reuse a non-empty
    idempotency_key when retrying the same write."""
    return _post(
        "/api/training/sessions",
        {
            "date": date,
            "entries": entries or [],
            "duration_minutes": duration_minutes,
            "notes": notes,
            "idempotency_key": idempotency_key or None,
        },
    )


@mcp.tool()
def update_training(
    session_id: int,
    date: str | None = None,
    duration_minutes: int | None = None,
    notes: str | None = None,
    entries: list[dict] | None = None,
) -> dict:
    """Edit a training session (ids from get_training). Only the supplied fields change;
    `entries`, when given, replaces every set of the session (same shape as log_training)."""
    fields = {"date": date, "duration_minutes": duration_minutes, "notes": notes, "entries": entries}
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        raise ToolError("update_training needs at least one field to change")
    return _patch(f"/api/training/sessions/{session_id}", payload)


@mcp.tool()
def delete_training(session_id: int) -> dict:
    """Delete a training session and its sets permanently."""
    return _delete(f"/api/training/sessions/{session_id}")


@mcp.tool()
def get_daily(date: str) -> dict:
    """One day of the daily log (YYYY-MM-DD): energy, routines, the four A.N.D.Y. tasks and
    notes. A day without an entry returns an error, not an empty log."""
    return _get(f"/api/daily/{date}")


@mcp.tool()
def delete_daily(date: str) -> dict:
    """Delete one day of the daily log (YYYY-MM-DD) permanently."""
    return _delete(f"/api/daily/{date}")


@mcp.tool()
def delete_freedom_day(habit: str, date: str) -> dict:
    """Delete one Freedom habit's day entry (`habit` slug from get_habits) and the relapse
    marker it created; relapses recorded another way stay. Requires the sensitive
    scope."""
    return _delete(f"/api/freedom/{_habit_slug(habit)}/days/{date}")


@mcp.tool()
def delete_feniks_day(date: str, habit: str = "") -> dict:
    """Deprecated: use delete_freedom_day. Empty `habit` means the first tracked habit."""
    return delete_freedom_day(habit, date)


@mcp.tool()
def update_brick(
    brick_id: int,
    date: str | None = None,
    hook: str | None = None,
    craving: int | None = None,
    story: str | None = None,
) -> dict:
    """Edit a Freedom brick (ids from get_habits or get_noporn). Only the supplied fields
    change; the hook cannot become blank. Requires the sensitive scope."""
    fields = {"date": date, "hook": hook, "craving": craving, "story": story}
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        raise ToolError("update_brick needs at least one field to change")
    return _patch(f"/api/freedom/bricks/{brick_id}", payload)


@mcp.tool()
def delete_brick(brick_id: int) -> dict:
    """Delete a Freedom brick permanently. Requires the sensitive scope."""
    return _delete(f"/api/freedom/bricks/{brick_id}")


@mcp.tool()
def update_habit(
    habit: str,
    name: str | None = None,
    slip_label: str | None = None,
    slip_definition: str | None = None,
    amount_unit: str | None = None,
    slip_threshold: int | None = None,
    near_miss_label: str | None = None,
    start_date: str | None = None,
    target_days: int | None = None,
    big_why: str | None = None,
) -> dict:
    """Change one Freedom habit's settings (`habit` slug from get_habits); only the supplied
    fields change. `name`; its definition of a slip: `slip_label` (the button, e.g. "I drank"),
    `slip_definition`, `amount_unit` and `slip_threshold` (the least amount that counts as a
    slip), `near_miss_label`; and `start_date` (YYYY-MM-DD, today or earlier), `target_days`
    (1-3650), `big_why`. The slug stays the same when the habit is renamed. Requires the
    sensitive scope."""
    fields = {
        "name": name,
        "slip_label": slip_label,
        "slip_definition": slip_definition,
        "amount_unit": amount_unit,
        "slip_threshold": slip_threshold,
        "near_miss_label": near_miss_label,
        "start_date": start_date,
        "target_days": target_days,
        "big_why": big_why,
    }
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        raise ToolError("update_habit needs at least one field to change")
    return _patch(f"/api/freedom/{_habit_slug(habit)}", payload)


@mcp.tool()
def update_feniks_config(
    start_date: str | None = None, target_days: int | None = None, big_why: str | None = None, habit: str = ""
) -> dict:
    """Deprecated: use update_habit. Changes one habit's start date, target and why; empty
    `habit` means the first tracked habit."""
    if start_date is None and target_days is None and big_why is None:
        raise ToolError("update_feniks_config needs at least one field to change")
    return update_habit(habit, start_date=start_date, target_days=target_days, big_why=big_why)


@mcp.tool()
def get_training_session(session_id: int) -> dict:
    """One training session with every set (movement, set number, reps, weight, duration,
    equipment, note)."""
    return _get(f"/api/training/sessions/{session_id}")


@mcp.tool()
def get_measurements(days: int = 90) -> dict:
    """Body measurements of the last N days (1-3650): weight in kg; arm, waist, hips and
    thighs in cm."""
    return _get("/api/measurements", params={"range": days})


@mcp.tool()
def log_measurements(
    date: str,
    weight: float | None = None,
    arm: float | None = None,
    waist: float | None = None,
    hips: float | None = None,
    thighs: float | None = None,
) -> dict:
    """Record body measurements for one date (YYYY-MM-DD): weight in kg, circumferences in
    cm. Only the supplied fields change; the rest of that date keeps its values."""
    fields = {"weight": weight, "arm": arm, "waist": waist, "hips": hips, "thighs": thighs}
    payload = {key: value for key, value in fields.items() if value is not None}
    if not payload:
        raise ToolError("log_measurements needs at least one measurement")
    return _put(f"/api/measurements/{date}", payload)


@mcp.tool()
def delete_measurements(date: str) -> dict:
    """Delete the body measurements of one date permanently."""
    return _delete(f"/api/measurements/{date}")


# --- Finance (the finance scope: a token with it, or VIRGIL_API_FINANCE=true for the server key) ---


@mcp.tool()
def get_finance_overview(date: str = "") -> dict:
    """The financial fortress on a date (YYYY-MM-DD, default today): each layer with
    its accounts, latest balances and as-of dates, the cushion and how many months
    of essential cost it covers, the shares of that cost covered by independent and
    by all stable income, insurance policies, and the score per layer and in total
    (points: independent income only; points_with_all_income: all stable income).
    Amounts are in the base currency. Accounts not updated in the latest snapshot
    are listed in stale_accounts. A null score means an input is missing; the layer
    says which."""
    return _get("/api/finance", params={"date": date} if date else None)


@mcp.tool()
def get_finance_history(months: int = 12) -> dict:
    """Totals per layer, the cushion and cushion months at every date balances were
    recorded in the last `months` months."""
    return _get("/api/finance/history", params={"months": months})


@mcp.tool()
def record_balances(date: str, entries: list[dict]) -> dict:
    """Record a whole snapshot in one call: the balance of each account on one date
    (YYYY-MM-DD), all or none. Each entry is {"account": name or id, "amount": value
    in the base currency} (a number is an account id, text is always a name),
    optionally with "native_amount" and "native_currency"
    for an account held in another currency, and "note". "amount": null removes that
    date's balance. Unknown account names are refused: create them with
    upsert_finance_account first. Returns how many were recorded."""
    return _put(f"/api/finance/balances/{quote(date, safe='')}", {"entries": entries})


@mcp.tool()
def upsert_finance_account(
    name: str,
    layer: str = "",
    institution: str | None = None,
    kind: str | None = None,
    currency: str | None = None,
    ownership: str | None = None,
    archived: bool | None = None,
    rename: str = "",
) -> dict:
    """Create a finance account or change the one with this name. `layer` is the
    fortress layer it belongs to (see get_finance_scoring); kind is one of cash,
    bonds, retirement, brokerage, crypto, property, other; ownership is own, joint
    or excluded (shown, never counted toward the cushion or totals). Archive an
    account that was closed; `rename` changes its name."""
    accounts = _get("/api/finance/accounts")["accounts"]
    match = next((account for account in accounts if account["name"].lower() == name.strip().lower()), None)
    payload: dict = {"name": rename or name}
    if match:
        payload["id"] = match["id"]
    if layer:
        payload["layer"] = layer
    for key, value in (
        ("institution", institution),
        ("kind", kind),
        ("currency", currency),
        ("ownership", ownership),
        ("archived", archived),
    ):
        if value is not None:
            payload[key] = value
    return _put("/api/finance/accounts", payload)


@mcp.tool()
def set_finance_assumptions(
    essential_monthly_cost: float | None = None,
    effective_from: str = "",
    income_sources: list[dict] | None = None,
    policies: list[dict] | None = None,
    base_currency: str = "",
) -> dict:
    """Set what the cushion and score are measured against. essential_monthly_cost
    applies from effective_from (default today), and earlier months keep their own.
    income_sources replaces the whole list: [{"name", "monthly_amount",
    "independent_of_work": true for income that does not depend on current work,
    "confirmed_on"}]. policies sets the types it names: [{"type": "life", "status":
    "present" | "absent" | "not_applicable", "confirmed_on", "note"}]; status null
    removes one. Returns everything currently set."""
    payload: dict = {}
    if essential_monthly_cost is not None:
        payload["essential_monthly_cost"] = essential_monthly_cost
    if effective_from:
        payload["effective_from"] = effective_from
    if income_sources is not None:
        payload["income_sources"] = income_sources
    if policies is not None:
        payload["policies"] = policies
    if base_currency:
        payload["base_currency"] = base_currency
    if not payload:
        return _get("/api/finance/assumptions")
    return _put("/api/finance/assumptions", payload)


@mcp.tool()
def get_finance_scoring() -> dict:
    """The fortress definition: every layer (name, description, whether it counts
    toward the cushion) and its score rule. A rule has a metric (cushion_months,
    independent_income_share in percent, or policies), max_points, and thresholds:
    [[minimum, points], ...] for the first two, {policy type: points} for policies."""
    return _get("/api/finance/scoring")


@mcp.tool()
def set_finance_scoring(layers: list[dict] | None = None, remove: list[str] | None = None) -> dict:
    """Create or change fortress layers and how they score. Each layer is matched by
    "name" (or "id") and may set "rename", "description", "counts_toward_cushion",
    "display_order" and "rule": {"metric", "max_points", "thresholds"} (see
    get_finance_scoring), or "rule": null to stop scoring it. `remove` deletes
    layers that no account uses. Returns the whole definition."""
    payload: dict = {}
    if layers is not None:
        payload["layers"] = layers
    if remove is not None:
        payload["remove"] = remove
    return _put("/api/finance/scoring", payload)


if __name__ == "__main__":
    mcp.run()
