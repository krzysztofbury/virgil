"""Regression: empty training sections must stay visible with their add-exercise form
(bug: deleting the last exercise in a section made the section — and the form — vanish)."""

import json
import shutil
import subprocess
from html.parser import HTMLParser

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


class _TrainingPageParser(HTMLParser):
    """Parses the real rendered page with an actual HTML tokenizer (not
    string search), so a change that breaks HTML attribute parsing — e.g. a
    regression from single- back to double-quoted x-data, which truncates the
    attribute at the JSON blob's own first internal `"` — is caught rather
    than silently missed by a search that's blind to quote style.

    Collects, per picker <label>:
      - x-data attribute values (the raw "exercisePicker([...])" text)
      - each <option>'s `:value` construction expression (the actual code
        that builds the onchange payload, inside <template x-for>)
      - each tag-filter chip's `@click` handler expression, keyed by its
        `data-tag-filter` value (the same text across sections, so a plain
        dict keyed by tag is enough)
    """

    def __init__(self):
        super().__init__()
        self.label_xdata: list[str] = []
        self.option_value_exprs: list[str] = []
        self.chip_click_by_tag: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        if tag == "label" and "x-data" in attr_map:
            self.label_xdata.append(attr_map["x-data"])
        if tag == "option" and ":value" in attr_map and "x-text" in attr_map:
            self.option_value_exprs.append(attr_map[":value"])
        if tag == "button" and "data-tag-filter" in attr_map and "@click" in attr_map:
            self.chip_click_by_tag.setdefault(attr_map["data-tag-filter"], attr_map["@click"])


def _parse_training_page(html: str) -> _TrainingPageParser:
    parser = _TrainingPageParser()
    parser.feed(html)
    return parser


def _js_string_literal(text: str) -> str:
    """A JS string literal (double-quoted, properly escaped) for `text` — safe
    to splice into generated JS source even when `text` itself contains quotes
    (e.g. an extracted `toggle('crossfit')` handler). json.dumps produces
    valid JS string-literal syntax, not just valid JSON."""
    return json.dumps(text)


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
    each library row's real metric ("t": it.metric) in the picker's movements
    data so there is something to fill it WITH. Deleting either one — the
    hidden input or the "t" key — leaves the confirm-route B3 test green
    because that test never renders or reads this page at all.

    This only pins the UPSTREAM data (movements_data, server-rendered into
    x-data). It does NOT exercise the client-side expression that actually
    builds the onchange payload from that data — see
    test_picker_onchange_payload_construction_carries_metric for that."""
    resp = auth_client.get("/training")
    assert resp.status_code == 200
    assert 'name="metric"' in resp.text, (
        "the add-exercise form needs a hidden `metric` field for the picker's onchange JS to fill"
    )
    # "Row" (migration 016/017, tagged 'crossfit', section='Cardio') is
    # seeded with metric='time' — its picker movements data must carry that
    # through as "t": "time", not omit the key.
    assert '"t": "time"' in resp.text, "the picker's movements data must carry the library row's real metric"


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
    text ">Back Squat<" no longer appears anywhere in server-rendered HTML —
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


def test_picker_movements_json_is_valid_html_attribute_and_json(auth_client):
    """Pins a bug caught (and fixed) during this task's own review: an x-data
    attribute embedding `{{ movements_data | tojson }}` MUST be single-quoted.
    Jinja's `tojson` HTML-safe escaping only escapes apostrophes (so the
    output is safe to embed in a SINGLE-quoted attribute) and deliberately
    leaves the JSON's own double quotes untouched, since JSON requires them.
    A double-quoted `x-data="exercisePicker({{ ... | tojson }})"` would have
    its attribute value truncated by the JSON blob's own first internal `"`
    in any real HTML parser, corrupting the expression and breaking Alpine's
    x-data silently — no traceback, no server-side signal at all, since the
    server-rendered HTML is technically well-formed UTF-8 text either way.

    Uses Python's html.parser.HTMLParser — a real tokenizer that follows HTML
    attribute-value quoting rules — specifically so a regression to
    double-quoted x-data is caught: reverting the quote style leaves this
    test's own extraction unable to find a well-formed
    "exercisePicker(<valid JSON>)" value (the parser would hand back a
    value truncated at the first embedded `"`), and it fails loudly instead
    of the four non-empty sections silently losing their picker.
    """
    html = auth_client.get("/training").text
    parser = _parse_training_page(html)
    assert len(parser.label_xdata) == 4, (
        f"expected one exercisePicker(...) x-data per non-empty section, got {len(parser.label_xdata)}: "
        f"{parser.label_xdata!r}"
    )

    for raw in parser.label_xdata:
        assert raw.startswith("exercisePicker(") and raw.endswith(")"), (
            f"x-data did not parse as one well-formed exercisePicker(...) call — "
            f"got {raw[:60]!r}...{raw[-40:]!r} (len {len(raw)})"
        )
        movements = json.loads(raw[len("exercisePicker(") : -1])
        assert isinstance(movements, list) and movements, "expected a non-empty movements array"

    core_blob = next(v for v in parser.label_xdata if '"n": "Back Squat"' in v)
    core_movements = json.loads(core_blob[len("exercisePicker(") : -1])
    names = [m["n"] for m in core_movements]
    assert names.count("Back Squat") == 1


@pytest.mark.skipif(NODE is None, reason="node not available — cannot execute the real onchange payload expression")
def test_picker_onchange_payload_construction_carries_metric(auth_client, tmp_path):
    """Before this task's rewrite, an <option>'s `value=` attribute WAS the
    final payload the browser hands to `onchange` — server-rendered, so
    pinning it in resp.text genuinely pinned the real thing. After the
    rewrite, that payload is instead assembled client-side, in the
    `:value="JSON.stringify({n:m.n,s:m.s,r:m.r,o:m.o,t:m.t})"` expression on
    the <option> inside <template x-for> (training.html). Every other test in
    this file only pins the UPSTREAM `movements_data` blob in x-data — none of
    them evaluate this expression, so dropping `t:m.t` from it left every
    other test green while silently breaking the exact contract that already
    had a historical bug once (the picker dropping `metric`, mis-typing
    movements and feeding erg strokes into the weekly rep count).

    This test extracts the REAL `:value` expression text (via the HTML
    tokenizer, not string search) and evaluates it with Node against an
    actual filtered movement, so it pins the construction's OUTPUT, not just
    its input.
    """
    html = auth_client.get("/training").text
    parser = _parse_training_page(html)
    assert parser.option_value_exprs, "no <option :value=...> found inside the picker's <template x-for>"
    value_expr = parser.option_value_exprs[0]

    picker_js = _extract_exercise_picker_js(html)
    movements_json = _extract_section_movements_json(html, '"n": "Back Squat"')

    script = tmp_path / "check_payload.js"
    script.write_text(
        picker_js
        + "\n"
        + "const movements = "
        + movements_json
        + ";\n"
        + "const picker = exercisePicker(movements);\n"
        + "const m = picker.filtered.find((x) => x.n === 'Back Squat');\n"
        + "if (!m) { throw new Error('Back Squat not found in filtered movements'); }\n"
        + "const payloadJson = (function (m) { return "
        + value_expr
        + "; })(m);\n"
        + "console.log(payloadJson);\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, f"node execution failed: {result.stderr}"
    payload = json.loads(result.stdout)

    assert payload["n"] == "Back Squat"
    assert "t" in payload, "the onchange payload construction must carry the movement's metric ('t')"
    assert payload["t"] == "reps", "Back Squat's real metric must survive into the actual onchange payload"


@pytest.mark.skipif(NODE is None, reason="node not available — cannot execute the real Alpine filter/chip handlers")
def test_picker_filter_predicate_actually_excludes_non_matching_movements(auth_client, tmp_path):
    """A server-rendered test cannot click a chip in a browser — there is no
    JS engine in this suite. This is the closest thing that can run without
    one: it slices the REAL `exercisePicker()` source, the REAL Core-section
    movements array, and the REAL chip `@click` handler text (both the
    crossfit chip's `toggle('crossfit')` and the "All" chip's
    `activeTag = ''`) out of the page /training actually returns, and
    executes all of it with Node. It calls `picker.toggle(...)` and evaluates
    the extracted "All"-chip expression itself (via `with(scope){...}`,
    approximating how Alpine resolves bare identifiers against the component)
    rather than poking `activeTag` directly — so this exercises the actual
    shipped click handlers, not a re-implementation of what they're assumed
    to do that could silently drift from the real thing.

    Proves: clicking the real crossfit chip's handler excludes a
    gym-classic-only movement (Barbell Row) and an untagged movement
    (Bent-over Row) from `.filtered`, while a crossfit-tagged movement (Back
    Squat) survives. Clicking the real "All" chip's handler afterwards clears
    `activeTag` and restores the full movement list — including both
    previously-excluded movements and the untagged ones (the ~20-row
    kettlebell program that carries no tag by design), which must stay
    reachable.

    Does NOT prove: that a real browser actually adds/removes <option> DOM
    nodes when the x-for source array changes, or that any specific browser
    (Safari included) renders the result correctly. That needs a JS-capable
    browser harness (e.g. Playwright), which this suite does not have.
    """
    html = auth_client.get("/training").text
    parser = _parse_training_page(html)
    crossfit_click_expr = parser.chip_click_by_tag.get("crossfit")
    all_click_expr = parser.chip_click_by_tag.get("__all__")
    assert crossfit_click_expr, "no crossfit chip's @click handler found"
    assert all_click_expr, 'no "All" chip\'s @click handler found'

    picker_js = _extract_exercise_picker_js(html)
    movements_json = _extract_section_movements_json(html, '"n": "Back Squat"')
    total_movements = len(json.loads(movements_json))

    script = tmp_path / "check_filter.js"
    script.write_text(
        picker_js
        + "\n"
        + "const movements = "
        + movements_json
        + ";\n"
        + "const picker = exercisePicker(movements);\n"
        + "(new Function('scope', "
        + _js_string_literal("with(scope){ " + crossfit_click_expr + " }")
        + "))(picker);\n"
        + "const crossfitNames = picker.filtered.map((m) => m.n).sort();\n"
        + "const activeTagAfterCrossfit = picker.activeTag;\n"
        + "(new Function('scope', "
        + _js_string_literal("with(scope){ " + all_click_expr + " }")
        + "))(picker);\n"
        + "const afterAllNames = picker.filtered.map((m) => m.n).sort();\n"
        + "console.log(JSON.stringify({\n"
        + "    crossfit: crossfitNames,\n"
        + "    activeTagAfterCrossfit: activeTagAfterCrossfit,\n"
        + "    afterAll: afterAllNames,\n"
        + "    activeTagAfterAll: picker.activeTag,\n"
        + "}));\n"
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, f"node execution failed: {result.stderr}"
    out = json.loads(result.stdout)

    assert out["activeTagAfterCrossfit"] == "crossfit", "clicking the real crossfit chip must set activeTag"
    assert "Back Squat" in out["crossfit"], "a crossfit-tagged movement must survive the crossfit filter"
    assert "Barbell Row" not in out["crossfit"], "a gym-classic-only movement must be excluded by the crossfit filter"
    assert "Bent-over Row" not in out["crossfit"], "an untagged movement must be excluded by an active tag filter"

    assert out["activeTagAfterAll"] == "", 'clicking the real "All" chip must clear activeTag'
    assert len(out["afterAll"]) == total_movements, 'the full movement list must be restored after clicking "All"'
    assert "Barbell Row" in out["afterAll"], 'a previously tag-filtered movement must be reachable again after "All"'
    assert "Bent-over Row" in out["afterAll"], (
        'untagged movements (the kettlebell program) must be reachable again after "All"'
    )
