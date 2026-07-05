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
