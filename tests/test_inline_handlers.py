"""No template value is ever written into inline JavaScript.

HTML escaping does not protect a JS string inside an event-handler attribute:
the browser decodes `&#39;` back to a quote before the script runs, so a marker
or exercise named with an apostrophe broke out of its confirm() string. And
`|tojson` output carries double quotes, which end the attribute itself. Values go
into data attributes instead, which the handler reads.
"""

import re
from pathlib import Path

HANDLER = re.compile(r"""\bon[a-z]+\s*=\s*(["'])(.*?)\1""", re.S)


def test_no_inline_handler_interpolates_a_template_value():
    offenders = []
    for template in Path("app/templates").rglob("*.html"):
        for match in HANDLER.finditer(template.read_text()):
            if "{{" in match.group(2) or "{%" in match.group(2):
                offenders.append(f"{template.name}: {match.group(0)[:80]}")
    assert offenders == []


def test_a_quote_in_an_exercise_name_stays_inside_its_confirm_text(auth_client):
    import sqlite3

    from tests.conftest import user_db_path

    conn = sqlite3.connect(user_db_path())
    exercise = conn.execute(
        "INSERT INTO exercise_library (name, section) VALUES (?, 'Core')", ('ZZ Farmer\'s "walk"',)
    ).lastrowid
    conn.commit()
    try:
        html = auth_client.get("/settings?tab=configuration").text
    finally:
        conn.execute("DELETE FROM exercise_library WHERE id = ?", (exercise,))
        conn.commit()
        conn.close()

    assert 'data-confirm="Delete ZZ Farmer&#39;s &#34;walk&#34; from the library?"' in html
