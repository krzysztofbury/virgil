"""Settings: the Exercise Library must be searchable, not just long.

It rendered the whole library as one editable table per section, with the add
form permanently open above it and no way to find a movement by name or tag.
"""

from pathlib import Path


def test_library_has_search_and_a_section_filter(auth_client):
    html = auth_client.get("/settings?tab=configuration").text
    assert 'x-data="libraryFilter"' in html
    assert 'type="search"' in html, "the library needs a search box"
    assert "data-library-name" in html, "rows must carry what the filter matches on"
    assert "data-library-tags" in html
    assert "data-library-section" in html
    assert "data-library-block" in html, "a collapsed section must be openable by a match"
    # x-data names a registered component. Without it the component throws on
    # init and the whole card loses its Alpine behaviour.
    components = Path("app/static/js/components.js").read_text()
    assert "Alpine.data('libraryFilter', libraryFilter)" in components, "the component named by x-data must exist"


def test_add_exercise_is_behind_a_disclosure(auth_client):
    html = auth_client.get("/settings?tab=configuration").text
    add = html.index("/settings/library/add")
    summary = html.rindex("<summary", 0, add)
    assert "Add exercise" in html[summary : summary + 80]


def test_rows_stay_visible_without_alpine(auth_client):
    """The filter is an enhancement. Every row is server-rendered and visible."""
    html = auth_client.get("/settings?tab=configuration").text
    assert "hidden" not in html.split("data-library-name")[1][:200], "no row may start hidden"
