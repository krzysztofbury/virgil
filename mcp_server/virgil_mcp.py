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
    VIRGIL_API_URL  — base URL, e.g. https://virgil.example.com (required)
    VIRGIL_API_KEY  — API key matching the server's VIRGIL_API_KEY (required)

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

mcp = FastMCP("virgil")


def _get(path: str, params: dict | None = None) -> dict:
    if not API_URL or not API_KEY:
        raise RuntimeError("Set VIRGIL_API_URL and VIRGIL_API_KEY environment variables")
    resp = httpx.get(
        f"{API_URL}{path}",
        params=params,
        headers={"X-API-Key": API_KEY},
        timeout=30.0,
    )
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


if __name__ == "__main__":
    mcp.run()
