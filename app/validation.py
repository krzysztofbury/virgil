"""Shared input validation helpers for form endpoints."""

from datetime import date


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
