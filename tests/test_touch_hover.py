"""Hover and transition contracts for touch screens (#201).

A touch browser applies :hover on tap and keeps it until the next tap
elsewhere, so an ungated hover style sticks to the control the person just
used. Every hover rule therefore sits inside `@media (hover: hover) and
(pointer: fine)`. `transition: all` (or a bare duration, which means the same)
animates whatever property changes next, layout included, so transitions name
their properties. tests/test_browser_smoke.py checks the pages in a browser;
these tests pin the stylesheet rules.
"""

import re
from pathlib import Path

CSS = Path("app/static/css/app.css")
TEMPLATES = Path("app/templates")
GATE = "(hover: hover) and (pointer: fine)"

# The vendored flatpickr stylesheet has its own ungated :hover rules. Gating
# these overrides would hand a tapped day back to flatpickr's light theme.
VENDOR_OVERRIDE = ".virgil-datepicker"
VENDOR_CSS = next(Path("app/static/vendor").glob("flatpickr-*.css"))


def _rules(css: str) -> list[tuple[list[str], str, str]]:
    """Every style rule as (enclosing at-rule preludes, selector, body)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out = []
    stack: list[str] = []
    start = 0
    for i, char in enumerate(css):
        if char == "{":
            stack.append(css[start:i].strip())
            start = i + 1
        elif char == "}":
            prelude = stack.pop()
            if not prelude.startswith("@"):
                body_start = css.rindex("{", 0, i) + 1
                out.append(([p for p in stack if p.startswith("@")], prelude, css[body_start:i]))
            start = i + 1
        elif char == ";" and not stack:
            start = i + 1
    return out


def _template_styles() -> dict[str, str]:
    styles = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        blocks = re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(encoding="utf-8"), flags=re.S)
        if blocks:
            styles[str(path)] = "\n".join(blocks)
    return styles


def _ungated_hover(css: str) -> list[str]:
    return [
        selector
        for media, selector, _ in _rules(css)
        if ":hover" in selector and not selector.startswith(VENDOR_OVERRIDE) and not any(GATE in m for m in media)
    ]


def test_every_hover_rule_waits_for_a_hovering_pointer():
    css = CSS.read_text(encoding="utf-8")
    hover_rules = [selector for _, selector, _ in _rules(css) if ":hover" in selector]
    assert len(hover_rules) > 40, "the parser no longer finds the stylesheet's hover rules"
    assert _ungated_hover(css) == []


def test_template_styles_gate_their_hover_rules_too():
    styles = _template_styles()
    assert styles, "no template carries a <style> block any more"
    assert {path: _ungated_hover(css) for path, css in styles.items() if _ungated_hover(css)} == {}


def test_only_the_vendor_overrides_stay_ungated():
    """The exemption covers a hover the vendored stylesheet really styles, not a
    new datepicker rule that could be gated."""
    css = CSS.read_text(encoding="utf-8")
    vendor = VENDOR_CSS.read_text(encoding="utf-8")
    exempt = [
        part.strip()
        for _, selector, _ in _rules(css)
        if selector.startswith(VENDOR_OVERRIDE)
        for part in selector.split(",")
        if ":hover" in part
    ]
    assert exempt
    assert [part for part in exempt if part.split()[-1] not in vendor] == []


TRANSITION = re.compile(r"(?<![-\w])transition\s*:\s*([^;}]+)")


def test_transitions_name_their_properties():
    sources = {str(CSS): CSS.read_text(encoding="utf-8"), **_template_styles()}
    offenders = []
    for path, css in sources.items():
        for value in TRANSITION.findall(re.sub(r"/\*.*?\*/", "", css, flags=re.S)):
            for part in value.split(","):
                first = part.split()[0] if part.split() else ""
                if first == "all" or re.fullmatch(r"[\d.]+m?s", first):
                    offenders.append((path, value.strip()))
    assert offenders == []
