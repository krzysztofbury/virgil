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


def _get(path: str, params: dict | None = None) -> dict:
    if not API_URL or not API_KEY:
        raise RuntimeError("Set VIRGIL_API_URL and VIRGIL_API_KEY environment variables")
    headers = {"X-API-Key": API_KEY}
    if CF_ID and CF_SECRET:  # Cloudflare Access gate in front of the app
        headers["CF-Access-Client-Id"] = CF_ID
        headers["CF-Access-Client-Secret"] = CF_SECRET
    resp = httpx.get(f"{API_URL}{path}", params=params, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


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
    """Active experiments with current week number, weekly target (min/max) and logged progress."""
    return _get("/api/experiments/active")


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


if __name__ == "__main__":
    mcp.run()
