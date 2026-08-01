"""Tag normalisation — one spelling per tag, whatever the user types."""

import pytest

from app.library_validation import MAX_TAGS_PER_ENTRY, LibraryWriteError, normalize_tag, normalize_tags


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


@pytest.mark.parametrize(
    "raw,expected",
    [
        # "ł" is an independent letter of the Polish alphabet, not a Latin
        # vowel with a diacritic -- NFKD does not decompose it, so an
        # NFKD-only fold leaves it for the character filter to delete
        # ("siłowy" -> "siowy", "ŁAWKA" -> "AWKA"). An explicit map has to
        # run before NFKD to catch it. Covered in both cases because the map
        # only has a lowercase key -- lowercasing happens first.
        ("siłowy", "silowy"),
        ("ŁAWKA", "lawka"),
        # Mixes an undecomposable letter (ż, ł) with letters NFKD DOES
        # decompose (ó) in one word, so both mechanisms must fire together.
        ("żółty", "zolty"),
        # Accented vowels: NFKD decomposes these into base letter + combining
        # mark, which the plain ascii encode then drops -- this path already
        # worked before the explicit map was added, and must keep working.
        ("ćwiczenia", "cwiczenia"),
        ("pięść", "piesc"),
    ],
)
def test_normalize_tag_transliterates_polish_to_ascii(raw, expected):
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


def test_normalize_tags_accepts_exactly_the_max_count():
    tags = [f"tag-{i}" for i in range(MAX_TAGS_PER_ENTRY)]
    assert normalize_tags(tags) == sorted(tags)


def test_normalize_tags_rejects_over_the_max_count():
    """B3 (2026-07-31 review): MAX_TAG_LEN bounded one tag's length; nothing
    bounded how many an entry could carry — 200,000 tags were measured
    accepted in 0.25s. _replace_tags (api.py) and settings.py's tag-replace
    both issue one INSERT per tag inside the write transaction, so this must
    raise BEFORE that loop even sees the list, not truncate it silently."""
    tags = [f"tag-{i}" for i in range(MAX_TAGS_PER_ENTRY + 1)]
    with pytest.raises(LibraryWriteError) as exc:
        normalize_tags(tags)
    assert exc.value.status == 422


def test_normalize_tags_count_limit_is_on_the_deduplicated_set():
    """The bound applies to distinct tags -- the rows _replace_tags is about
    to INSERT -- not to the raw (pre-dedup) input length. A long list that
    collapses to a small distinct set must still be accepted."""
    tags = ["kettlebell"] * (MAX_TAGS_PER_ENTRY * 5)
    assert normalize_tags(tags) == ["kettlebell"]
