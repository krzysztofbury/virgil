"""No template carries an inline event handler, and values reach scripts as data.

The Content-Security-Policy refuses inline handlers outright, so one would
silently do nothing. They were also unsafe: the browser decodes `&#39;` back to
a quote before a handler runs, so a marker or exercise named with an apostrophe
broke out of its confirm() string. Values go into data attributes, which the
listeners in app.js read.
"""

import re
from pathlib import Path

HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""")


def test_no_template_has_an_inline_event_handler():
    offenders = [
        f"{template.name}: {line.strip()[:80]}"
        for template in sorted(Path("app/templates").rglob("*.html"))
        for line in template.read_text().splitlines()
        if HANDLER.search(line)
    ]
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
