"""Mobile layout contracts.

The 2026-08-27 audit found values clipped inside `overflow: hidden` cards. The
Training KPIs had the mechanism that matters: a page-level inline column count
outranks the stylesheet's own mobile rule. These tests pin the rules and the
absence of that override, which is what a browser pass cannot do in CI.
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


def test_stat_cards_shrink_on_mobile():
    block = _mobile_block()
    assert ".stat-grid { grid-template-columns: repeat(2, 1fr); }" in block, "the two-column mobile rule must stay"
    assert ".stat-card {" in block, "stat cards must get smaller padding on a phone"
    assert ".stat-value {" in block, "the 1.75rem mono value does not fit two-up at 320px"


def test_stat_cards_may_shrink_below_their_content():
    """A grid item defaults to min-width:auto, so a long value pushes its track
    wider than the card and the card clips it."""
    css = CSS.read_text(encoding="utf-8")
    stat_card = css[css.index(".stat-card {") : css.index(".stat-card:hover")]
    assert "min-width: 0" in stat_card
