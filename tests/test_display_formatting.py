"""Display formatting for lab reference ranges and counts."""

import pytest

from app.formatting import format_ref_range, format_thousands


@pytest.mark.parametrize(
    ("low", "high", "expected"),
    [
        (30.0, 100.0, "30-100"),
        (4.0, 5.6, "4-5.6"),
        (None, 100.0, "< 100"),  # rendered "-100.0" before: a negative bound
        (30.0, None, "> 30"),
        (0.0, 5.0, "0-5"),  # zero is a bound, not a missing one
        (None, None, ""),
    ],
)
def test_ref_range(low, high, expected):
    assert format_ref_range(low, high) == expected


@pytest.mark.parametrize(("value", "expected"), [(11089, "11,089"), (11089.0, "11,089"), (950, "950"), (None, "")])
def test_thousands(value, expected):
    assert format_thousands(value) == expected
