"""Regression: empty training sections must stay visible with their add-exercise form
(bug: deleting the last exercise in a section made the section — and the form — vanish)."""

import json
import shutil
import subprocess

import pytest

SECTIONS = ("Warmup", "Core", "Cardio", "Stretching")

NODE = shutil.which("node")


def _extract_balanced(text: str, start: int, open_char: str, close_char: str) -> int:
    """Index just past the char matching text[start] (== open_char), skipping
    JSON string contents so a brace/bracket inside a quoted string is never
    mistaken for real nesting."""
    assert text[start] == open_char
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        c = text[i]
        if in_string:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    raise ValueError(f"unbalanced {open_char!r}/{close_char!r} starting at {start}")


def _extract_exercise_picker_js(html: str) -> str:
    """The REAL exercisePicker() source, sliced out of the page's own <script>
    block — not a hand-copied re-implementation, so a test built on this
    actually exercises what ships."""
    marker = "function exercisePicker(movements) {"
    start = html.index(marker)
    brace_idx = html.index("{", start)
    end = _extract_balanced(html, brace_idx, "{", "}")
    return html[start:end]


def _extract_section_movements_json(html: str, needle: str) -> str:
    """Raw JSON array text for the section whose x-data attribute embeds `needle`
    (e.g. a specific movement's JSON, unique to one section's picker)."""
    idx = html.index(needle)
    call = html.rindex("exercisePicker(", 0, idx)
    array_start = html.index("[", call)
    array_end = _extract_balanced(html, array_start, "[", "]")
    return html[array_start:array_end]


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

    WebKit does not honour hidden/display:none/visibility:hidden on <option>
    elements inside a <select> (confirmed —
    https://github.com/mdn/browser-compat-data/issues/16619), so the picker no
    longer server-renders a static <option> per movement and toggles its
    visibility; instead training.html embeds one JSON array of movements per
    section (in the `x-data='exercisePicker([...])'` attribute) and Alpine's
    `x-for` generates the real <option> nodes client-side, so a filtered-out
    movement is never inserted into the DOM at all. That means the literal
    text "<Back Squat<" no longer appears anywhere in server-rendered HTML —
    what this test can still verify server-side is that the underlying data
    Alpine will render from carries Back Squat exactly once, not once per tag.

    Asserts == 1, not <= 1: a picker whose movements array was emptied
    entirely would satisfy <= 1 and the test would pass while the feature
    was gone.
    """
    page = auth_client.get("/training").text
    protocol = page.split("Personal Bests")[0]
    assert protocol.count('"n": "Back Squat"') == 1, "exactly one movement entry, tags notwithstanding"


def test_picker_exposes_tag_filter_chips(auth_client):
    """Assert on the chip's own markup, not on the bare word: 'crossfit'
    appears elsewhere on the page regardless of whether a chip rendered.

    The second assertion targets the actual mechanism the filter now runs on:
    each movement's own `tags` array, embedded once per section in the
    `x-data='exercisePicker([...])'` JSON blob (see
    test_picker_renders_each_movement_exactly_once for why <option>-level
    `data-tags` no longer exists — Alpine generates <option>s from this array,
    it does not read attributes back off already-rendered ones).
    """
    page = auth_client.get("/training").text
    assert 'data-tag-filter="crossfit"' in page, "a chip must render for a tag present in the section"
    assert '"tags": ["crossfit", "gym-classic"]' in page, (
        "Back Squat's own tags must be embedded in the movements data the filter reads"
    )


@pytest.mark.skipif(NODE is None, reason="node not available — cannot execute the real Alpine filter predicate")
def test_picker_filter_predicate_actually_excludes_non_matching_movements(auth_client, tmp_path):
    """A server-rendered test cannot click a chip in a browser — there is no
    JS engine in this suite. This is the closest thing that can run without
    one: it slices the REAL `exercisePicker()` source and the REAL Core-section
    movements array out of the page /training actually returns (both are
    literally present in the HTML), and executes them with Node. That
    exercises the actual shipped filtering logic, not a hand-copied
    re-implementation of it that could silently drift from the real thing.

    Proves: with 'crossfit' active, a gym-classic-only movement (Barbell Row)
    and an untagged movement (Bent-over Row) are excluded from `.filtered`,
    while a crossfit-tagged movement (Back Squat) survives; with no active
    tag, untagged movements are NOT excluded — the ~20 kettlebell-program rows
    that carry no tag by design must stay reachable.

    Does NOT prove: that a real browser actually adds/removes <option> DOM
    nodes when the x-for source array changes, or that any specific browser
    (Safari included) renders the result correctly. That needs a JS-capable
    browser harness (e.g. Playwright), which this suite does not have.
    """
    html = auth_client.get("/training").text
    picker_js = _extract_exercise_picker_js(html)
    movements_json = _extract_section_movements_json(html, '"n": "Back Squat"')

    script = tmp_path / "check_filter.js"
    script.write_text(
        picker_js
        + "\n"
        + "const movements = "
        + movements_json
        + ";\n"
        + "const picker = exercisePicker(movements);\n"
        + "function namesFor(tag) {\n"
        + "    picker.activeTag = tag;\n"
        + "    return picker.filtered.map((m) => m.n).sort();\n"
        + "}\n"
        + "console.log(JSON.stringify({ crossfit: namesFor('crossfit'), none: namesFor('') }));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, f"node execution failed: {result.stderr}"
    out = json.loads(result.stdout)

    assert "Back Squat" in out["crossfit"], "a crossfit-tagged movement must survive the crossfit filter"
    assert "Barbell Row" not in out["crossfit"], "a gym-classic-only movement must be excluded by the crossfit filter"
    assert "Bent-over Row" not in out["crossfit"], "an untagged movement must be excluded by an active tag filter"

    assert "Bent-over Row" in out["none"], (
        "untagged movements (the kettlebell program) must stay reachable with no filter active"
    )
    assert "Back Squat" in out["none"], "with no filter active every movement must be reachable"
