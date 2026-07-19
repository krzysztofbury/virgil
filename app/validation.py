"""Shared input validation helpers for form endpoints."""

from datetime import date
from typing import Annotated

from fastapi import Form
from pydantic import BeforeValidator


def _blank_to_none(v):
    """HTML forms submit '' for untouched number inputs — treat as None."""
    if isinstance(v, str) and not v.strip():
        return None
    return v


def _blank_to_none_decimal(v):
    """Like _blank_to_none, plus tolerate the Polish decimal comma ('93,5')."""
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
        return v.replace(",", ".")
    return v


# Use as `x: OptionalFormInt = None` — blank input becomes None instead of 422.
# Form() must live INSIDE Annotated: with `= Form(None)` FastAPI ignores the validators
# (verified empirically against this FastAPI version).
OptionalFormInt = Annotated[int | None, BeforeValidator(_blank_to_none), Form()]
OptionalFormFloat = Annotated[float | None, BeforeValidator(_blank_to_none_decimal), Form()]


# --- Experiment metrics ---------------------------------------------------
# Shared by the experiments router (forms) and the REST API (MCP writes).

METRIC_KINDS = ("duration", "count", "boolean", "scale")
TARGET_PERIODS = ("day", "week", "total")

_METRIC_VALUE_BOUNDS = {
    "duration": (1, 1440),  # minutes; a day has 1440
    "count": (1, 1000),  # events per single log
    "boolean": (0, 1),
    "scale": (0, 10),
}


def clamp_metric_value(kind: str, value: int) -> int | None:
    """Validate an entry value against its metric kind's bounds. None = reject."""
    lo, hi = _METRIC_VALUE_BOUNDS.get(kind, (1, 1440))
    if lo <= value <= hi:
        return value
    return None


def valid_date(s: str) -> bool:
    """Return True if s is a valid ISO date string."""
    try:
        date.fromisoformat(s)
        return True
    except (ValueError, TypeError):
        return False


def valid_month(s: str) -> bool:
    """Return True if s matches YYYY-MM format."""
    if not s or len(s) != 7 or s[4] != "-":
        return False
    try:
        int(s[:4])
        m = int(s[5:])
        return 1 <= m <= 12
    except ValueError:
        return False


def clamp(val: int | float | None, lo: int | float, hi: int | float) -> int | float | None:
    """Clamp a numeric value to [lo, hi], or return None if val is None."""
    if val is None:
        return None
    return max(lo, min(hi, val))


def truncate(s: str, max_len: int = 5000) -> str:
    """Truncate a string to max_len characters. Returns empty string for falsy input."""
    if not s:
        return ""
    return s[:max_len]


def clamp_float(raw: str, minimum: float, maximum: float) -> float:
    """Parse a string as float and clamp to [minimum, maximum]. Returns minimum on failure."""
    try:
        value = float(raw)
    except (ValueError, TypeError):
        return minimum
    return max(minimum, min(maximum, value))


def clamp_int(raw: str, minimum: int, maximum: int) -> int:
    """Parse a string as int and clamp to [minimum, maximum]. Returns minimum on failure."""
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return minimum
    return max(minimum, min(maximum, value))
