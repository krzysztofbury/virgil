# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mcp>=1.2",
#     "httpx>=0.27",
# ]
# ///
"""Virgil MCP server — thin stdio wrapper over the Virgil REST API.

Exposes Virgil data (daily habits, Oura, streaks, experiments, training)
to MCP clients (Claude Code, OpenClaw). No local DB access — everything
goes through the authenticated REST API, so it works from any machine.

Config (env):
    VIRGIL_API_URL          — base URL, e.g. https://virgil.example.com (required)
    VIRGIL_API_KEY          — API key matching the server's VIRGIL_API_KEY (required)
    CF_ACCESS_CLIENT_ID     — Cloudflare Access service token id (if host sits behind CF Access)
    CF_ACCESS_CLIENT_SECRET — Cloudflare Access service token secret

Run:
    uv run mcp_server/virgil_mcp.py

Register in Claude Code:
    claude mcp add virgil \
      -e VIRGIL_API_URL=https://virgil.example.com \
      -e VIRGIL_API_KEY=<key> \
      -- uv run /path/to/virgil/mcp_server/virgil_mcp.py
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.environ.get("VIRGIL_API_URL", "").rstrip("/")
API_KEY = os.environ.get("VIRGIL_API_KEY", "")
CF_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")

mcp = FastMCP("virgil")


def _headers() -> dict:
    if not API_URL or not API_KEY:
        raise RuntimeError("Set VIRGIL_API_URL and VIRGIL_API_KEY environment variables")
    headers = {"X-API-Key": API_KEY}
    if CF_ID and CF_SECRET:  # Cloudflare Access gate in front of the app
        headers["CF-Access-Client-Id"] = CF_ID
        headers["CF-Access-Client-Secret"] = CF_SECRET
    return headers


def _get(path: str, params: dict | None = None) -> dict:
    resp = httpx.get(f"{API_URL}{path}", params=params, headers=_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = httpx.post(f"{API_URL}{path}", json=payload, headers=_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _patch(path: str, payload: dict) -> dict:
    resp = httpx.patch(f"{API_URL}{path}", json=payload, headers=_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> dict:
    resp = httpx.delete(f"{API_URL}{path}", headers=_headers(), timeout=30.0)
    resp.raise_for_status()
    return {"deleted": True}


@mcp.tool()
def get_today_summary() -> dict:
    """Today's snapshot: daily habits + energy, no-porn streak, latest Oura vitals,
    training sessions this week, latest body measurements."""
    return _get("/api/summary")


@mcp.tool()
def get_oura_stats() -> dict:
    """Latest synced Oura vitals: sleep/readiness/activity scores, HRV, resting HR, steps."""
    return _get("/api/oura/today")


@mcp.tool()
def get_streaks() -> dict:
    """No-porn module status: days since last relapse and last relapse date."""
    return _get("/api/summary")["feniks"]


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
    Use when the volume summary from get_training isn't enough and you need what was actually done."""
    return _get("/api/training/detail", params={"range": days})


@mcp.tool()
def get_noporn(days: int = 30) -> dict:
    """No-porn (Feniks) detail for the last N days (1-365): config, streak, current-week clean
    rate, plus relapse/reset events, journal entries (emotions/triggers/thoughts/coping) and
    logged pleasures. Use this to see WHY relapses happened — get_streaks only gives the count."""
    return _get("/api/noporn", params={"range": days})


@mcp.tool()
def get_exercise_library(category: str = "", include_archived: bool = False) -> dict:
    """The exercise dictionary: every movement the training picker offers and the
    WOD parser is allowed to recognise. Pass category='CrossFit' for the WOD
    vocabulary. Entries marked builtin=1 can only be archived, not edited or deleted."""
    params: dict = {"include_archived": include_archived}
    if category:
        params["category"] = category
    return _get("/api/library", params=params)


@mcp.tool()
def add_exercise(
    category: str,
    section: str,
    name: str,
    metric: str = "reps",
    sets: int | None = None,
    reps: str = "",
    notes: str = "",
) -> dict:
    """Add a movement to the exercise dictionary. `section` is Warmup/Core/Cardio/
    Stretching — Core+reps feeds weekly volume and personal bests, Cardio does not.
    `metric` is 'reps' or 'time' (time entries log weight+seconds and are excluded
    from rep-based aggregates). Adding to category 'CrossFit' also teaches the WOD parser."""
    return _post(
        "/api/library",
        {
            "category": category,
            "section": section,
            "name": name,
            "metric": metric,
            "sets": sets,
            "reps": reps,
            "notes": notes,
        },
    )


@mcp.tool()
def update_exercise(
    entry_id: int,
    name: str = "",
    section: str = "",
    metric: str = "",
    reps: str = "",
    notes: str = "",
    archived: int | None = None,
) -> dict:
    """Edit one dictionary entry — see get_exercise_library for ids. Only the fields
    you pass change. A builtin entry refuses everything except `archived`."""
    payload = {
        k: v
        for k, v in (
            ("name", name),
            ("section", section),
            ("metric", metric),
            ("reps", reps),
            ("notes", notes),
        )
        if v
    }
    if archived is not None:
        payload["archived"] = archived
    return _patch(f"/api/library/{entry_id}", payload)


@mcp.tool()
def delete_exercise(entry_id: int) -> dict:
    """Remove a dictionary entry permanently. Builtin entries refuse deletion —
    archive them instead. Deleting does NOT touch logged training history."""
    return _delete(f"/api/library/{entry_id}")


if __name__ == "__main__":
    mcp.run()
