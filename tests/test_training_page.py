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
    resp = auth_client.get("/training")
    assert "From library" in resp.text
    assert "Workout A (KB full-body)" in resp.text


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
    # "Row" (migration 016/017, category='CrossFit', section='Cardio') is
    # seeded with metric='time' — its picker <option> JSON must carry that
    # through as "t": "time", not omit the key.
    assert '"t": "time"' in resp.text, "the picker option JSON must carry the library row's real metric"
