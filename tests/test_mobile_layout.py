"""Mobile layout contracts.

The 2026-08-27 audit found values clipped inside `overflow: hidden` cards. The
Training KPIs had the mechanism that matters: a page-level inline column count
outranks the stylesheet's own mobile rule. These tests pin the rules and the
absence of that override; tests/test_browser_smoke.py checks the pages
themselves at phone width for overflow and clipped cards.
"""

import re
from pathlib import Path

CSS = Path("app/static/css/app.css")
TEMPLATES = Path("app/templates")

# A FIXED inline column count is the defeat-the-media-query mechanism. An inline
# `repeat(auto-fill, minmax(...))` is responsive by construction (settings.html
# uses one for a checkbox list and it collapses correctly at 320px), so banning
# every inline grid would force churn on markup that has no defect.
INLINE_FIXED_COLUMNS = re.compile(r"style=\"[^\"]*grid-template-columns:\s*repeat\(\s*\d+", re.IGNORECASE)


def _mobile_block() -> str:
    """The body of the LAST `@media (max-width: 768px)` block, braces matched.

    The stylesheet opens a narrow-nav block at the top with the same query, and
    component rules continue after the Responsive section, so a slice that runs
    to the end of the file would contain every base rule and make each assertion
    below vacuous.
    """
    css = CSS.read_text(encoding="utf-8")
    start = css.rindex("@media (max-width: 768px)")
    open_brace = css.index("{", start)
    depth = 0
    for i in range(open_brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace + 1 : i]
    raise AssertionError("the responsive media block is not closed")


def test_no_template_pins_a_fixed_column_count_inline():
    """An inline fixed column count outranks every media query.

    That is how the Training KPIs came to render three columns at 390 px inside
    a card that hides its overflow.
    """
    offenders = [
        path.name
        for path in sorted(TEMPLATES.rglob("*.html"))
        if INLINE_FIXED_COLUMNS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"a fixed inline column count defeats the mobile rules: {offenders}"


def test_card_headers_wrap_helper_copy_on_mobile():
    """A title and a long helper sentence cannot share one flex row at 390px.

    Settings App Config puts both in one `.card-header`, so the title collapsed
    into a one-word-per-line column. Some headers also carry a control (the Notes
    Edit button, the Oura metric select), which must stay beside its title: the
    header wraps, and the helper `small` alone takes a full row.
    """
    block = _mobile_block()
    start = block.index(".card-header {")
    header_rule = block[start : block.index("}", start)]
    assert "flex-wrap: wrap" in header_rule
    assert "flex-direction: column" not in header_rule, "a column pushes every control under its title"
    assert ".card-header > small { flex-basis: 100%;" in block, "helper copy must get its own row"
