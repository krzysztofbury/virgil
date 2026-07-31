"""Tag normalisation — one spelling per tag, whatever the user types."""

import pytest

from app.library_validation import LibraryWriteError, normalize_tag, normalize_tags


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("kettlebell", "kettlebell"),
        ("Kettlebell", "kettlebell"),
        ("KETTLEBELL", "kettlebell"),
        ("  kettlebell  ", "kettlebell"),
        ("Kettle Bell", "kettle-bell"),
        ("Gym classics", "gym-classics"),
        ("gym   classics", "gym-classics"),
        ("gym--classics", "gym-classics"),
        ("gym---classics", "gym-classics"),
        ("-crossfit-", "crossfit"),
        ("Workout A (KB full-body)", "workout-a-kb-full-body"),
        ("hyrox!!", "hyrox"),
    ],
)
def test_normalize_tag_collapses_spellings(raw, expected):
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "!!!", "---"])
def test_normalize_tag_rejects_empty_result(raw):
    with pytest.raises(LibraryWriteError) as exc:
        normalize_tag(raw)
    assert exc.value.status == 422


def test_normalize_tag_rejects_over_length():
    with pytest.raises(LibraryWriteError):
        normalize_tag("x" * 41)


def test_normalize_tag_accepts_at_the_bound():
    assert normalize_tag("x" * 40) == "x" * 40


def test_normalize_tags_dedupes_and_sorts():
    # Input is deliberately out of sorted order AND carries a duplicate in different casing.
    # This single case pins both properties:
    #   - drop sorted()        -> insertion order ["yoga", "kettlebell", "crossfit"], fails
    #   - drop deduplication   -> "kettlebell" appears twice, fails
    assert normalize_tags(["yoga", "KETTLEBELL", "crossfit", "kettlebell"]) == [
        "crossfit",
        "kettlebell",
        "yoga",
    ]


def test_normalize_tags_from_comma_string():
    assert normalize_tags("crossfit, Kettle Bell , ") == ["crossfit", "kettle-bell"]


def test_normalize_tags_empty_input_is_empty_list():
    assert normalize_tags([]) == []
    assert normalize_tags("") == []


def test_normalize_tags_none_input_is_empty_list():
    assert normalize_tags(None) == []


def test_normalize_tags_raises_on_non_blank_item_that_normalises_to_nothing():
    """A non-blank item that normalises to nothing must raise, not silently drop."""
    with pytest.raises(LibraryWriteError) as exc:
        normalize_tags(["crossfit", "!!!", "kettlebell"])
    assert exc.value.status == 422


def test_normalize_tags_drops_blank_items_silently():
    """Blank items (empty strings, whitespace-only) are dropped, not an error."""
    assert normalize_tags(["crossfit", "", "  ", "kettlebell"]) == ["crossfit", "kettlebell"]
