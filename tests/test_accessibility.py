"""Keyboard and assistive-technology contracts every page shares.

The behaviour itself (tab order, focus rings, Escape returning focus) was
checked in a real browser; these pin the markup that behaviour depends on.
"""

import re
import sqlite3
from pathlib import Path

import pytest

from app.db import LIFE_AREAS
from tests.conftest import user_db_path


@pytest.mark.parametrize("page", ["/", "/daily", "/settings"])
def test_the_first_focusable_element_skips_to_the_page_content(auth_client, page):
    html = auth_client.get(page).text
    body = html[html.index("<body") :]

    first_link = re.search(r"<a\b[^>]*>", body).group(0)
    assert 'class="skip-link"' in first_link and 'href="#main-content"' in first_link
    assert re.search(r'<main class="container" id="main-content" tabindex="-1">', html)


def test_every_chart_has_a_text_alternative():
    """A canvas is a picture to a screen reader: each one says what it shows, or where the numbers are."""
    unlabelled = []
    for template in Path("app/templates").rglob("*.html"):
        for canvas in re.findall(r"<canvas\b[^>]*>", template.read_text(), flags=re.S):
            if 'role="img"' not in canvas or "aria-label=" not in canvas:
                unlabelled.append(f"{template.name}: {canvas[:60]}")
    assert unlabelled == []


def test_the_radar_reads_out_each_assessment(auth_client):
    """The radar has no table of its own, so its label carries the numbers."""
    conn = sqlite3.connect(user_db_path())
    day = "2999-01-01"  # newer than anything else, so it is the radar's first dataset
    conn.execute(
        f"INSERT INTO life_scores (date, {', '.join(LIFE_AREAS)}) VALUES (?{', ?' * len(LIFE_AREAS)})",  # noqa: S608
        (day, *range(1, len(LIFE_AREAS) + 1)),
    )
    conn.commit()
    try:
        html = auth_client.get("/").text
    finally:
        conn.execute("DELETE FROM life_scores WHERE date = ?", (day,))
        conn.commit()
        conn.close()

    label = re.search(r'<canvas id="radarChart" role="img" aria-label="([^"]*)"', html).group(1)
    assert label.startswith(
        "Life scores on 2999-01-01: Planning 1, Spirituality 2, Health 3, Work 4, Social 5, Growth 6, Relaxation 7, Family 8."
    )


def test_reduced_motion_stops_animation_scrolling_and_chart_drawing():
    css = Path("app/static/css/app.css").read_text()
    charts = Path("app/static/js/charts.js").read_text()

    assert "scroll-behavior: smooth" in css[css.index("prefers-reduced-motion: no-preference") :][:120]
    reduce = css[css.index("@media (prefers-reduced-motion: reduce) {\n  *, *::before") :][:300]
    assert "animation-duration: 0.01ms !important" in reduce and "transition-duration: 0.01ms !important" in reduce
    assert "prefers-reduced-motion: reduce" in charts and "Chart.defaults.animation = false" in charts
