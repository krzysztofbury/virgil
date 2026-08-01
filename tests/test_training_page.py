"""Regression: empty training sections must stay visible with their add-exercise form
(bug: deleting the last exercise in a section made the section — and the form — vanish)."""

SECTIONS = ("Warmup", "Core", "Cardio", "Stretching")


def test_all_sections_visible_even_when_empty(auth_client):
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    for section in SECTIONS:
        assert section in resp.text, f"Section {section} missing from training page"


def test_add_exercise_form_present_per_section(auth_client):
    resp = auth_client.get("/training")
    add_forms = resp.text.count('action="/training/exercise"')
    assert add_forms >= len(SECTIONS), f"Expected ≥{len(SECTIONS)} add-exercise forms, got {add_forms}"


def test_exercise_library_picker_present(auth_client):
    """Migration 019 removed `category` — the picker no longer groups by it
    (Task 2 falls back to a flat alphabetical list), so this now asserts on an
    actual exercise NAME rather than the category label the picker used to
    render as an <optgroup>.

    Asserting on the bare substring "Goblet Squat" would pass even with the
    entire picker <option> loop deleted: the add-exercise form's name input
    has `placeholder="e.g. Goblet Squat"` (training.html) unconditionally, on
    every section, whether or not the library ever renders. Assert on the
    picker's own JSON payload shape instead, which only exists per <option>."""
    resp = auth_client.get("/training")
    assert "From library" in resp.text
    assert '"n": "Goblet Squat"' in resp.text


def test_exercise_library_picker_carries_metric_to_the_hidden_field(auth_client):
    """B3 (template half): the confirm-route test for B3 posts `metric` by
    hand, so it never exercises the actual browser wiring that produces it —
    training.html's per-section add-exercise form must (1) carry a hidden
    `metric` input for the picker's onchange handler to fill, and (2) embed
    each library row's real metric ("t": it.metric) in the picker <option>
    JSON so there is something to fill it WITH. Deleting either one — the
    hidden input or the "t" key — leaves the confirm-route B3 test green
    because that test never renders or reads this page at all."""
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    assert 'name="metric"' in resp.text, (
        "the add-exercise form needs a hidden `metric` field for the picker's onchange JS to fill"
    )
    # "Row" (migration 016/017, tagged 'crossfit', section='Cardio') is
    # seeded with metric='time' — its picker <option> JSON must carry that
    # through as "t": "time", not omit the key.
    assert '"t": "time"' in resp.text, "the picker option JSON must carry the library row's real metric"


def test_picker_renders_each_movement_exactly_once(auth_client):
    """A movement carrying two tags used to appear under each category optgroup.

    Asserts == 1, not <= 1: a picker that stopped rendering movements
    altogether would satisfy <= 1 and the test would pass while the feature
    was gone.
    """
    page = auth_client.get("/training").text
    protocol = page.split("Personal Bests")[0]
    assert protocol.count(">Back Squat<") == 1, "exactly one option per movement, tags notwithstanding"


def test_picker_exposes_tag_filter_chips(auth_client):
    """Assert on the chip's own markup, not on the bare word: 'crossfit'
    appears elsewhere on the page regardless of whether a chip rendered.
    """
    page = auth_client.get("/training").text
    assert 'data-tag-filter="crossfit"' in page, "a chip must render for a tag present in the section"
    assert "data-tags=" in page, "options must carry their tags for the filter to act on"
