"""Keep the Polish catalog in step with the source (#152).

    uv run --locked --extra dev python scripts/i18n.py update

Extracts every marked string from app/ and merges it into
app/locale/pl/LC_MESSAGES/messages.po. New entries arrive with an empty
translation; tests/test_i18n_catalog.py fails until each one is translated.
The header is fixed text, so an update only changes the entries.
"""

import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "app" / "locale" / "pl" / "LC_MESSAGES" / "messages.po"
HEADER = b"""# Polish translation of the Virgil UI (#152).
#
# Tone: informal second person ("Zapisz", "Twoje cele").
# Glossary, kept as is: Virgil, A.N.D.Y., Freedom, Oura, WOD, MCP, API.
# Fixed Polish renderings are listed here as they are chosen.
msgid ""
msgstr ""
"Project-Id-Version: Virgil\\n"
"Language: pl\\n"
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=utf-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Plural-Forms: nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\\n"
"""


def extract(pot: Path) -> None:
    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "-q", "extract", "-F", "babel.cfg", "-o", str(pot), "app"],
        cwd=ROOT,
        check=True,
    )


def update() -> None:
    with tempfile.TemporaryDirectory() as scratch:
        pot = Path(scratch) / "messages.pot"
        extract(pot)
        with pot.open("rb") as handle:
            template = read_po(handle)
    with CATALOG.open("rb") as handle:
        catalog = read_po(handle)
    catalog.update(template, no_fuzzy_matching=True)
    body = BytesIO()
    write_po(body, catalog, width=0, omit_header=True, sort_by_file=True, ignore_obsolete=True, include_lineno=False)
    entries = body.getvalue().strip()
    CATALOG.write_bytes(HEADER + (b"\n" + entries + b"\n" if entries else b""))


if __name__ == "__main__":
    if sys.argv[1:] != ["update"]:
        sys.exit(__doc__)
    update()
