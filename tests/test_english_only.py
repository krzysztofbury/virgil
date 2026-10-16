"""The UI is English only: no Polish in templates, scripts or route messages.

The markdown importer (it parses the person's own Polish notes) and the library's
transliteration table are input handling, not UI, and are out of scope here.
"""

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
# Polish letters, plus common Polish UI words that have none ("Zapisz trening").
POLISH = re.compile(
    r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ„”]|\b(zapisz|trening|notatk\w*|wpis\w*|serie|sesj\w*|minuty|opcjonalnie|albo)\b",
    re.IGNORECASE,
)


def _offenders(paths, read) -> list[str]:
    found = []
    for path in paths:
        for lineno, text in read(path):
            if POLISH.search(text):
                found.append(f"{path.relative_to(APP)}:{lineno}: {text.strip()[:80]}")
    return found


def _lines(path):
    return enumerate(path.read_text(encoding="utf-8").splitlines(), 1)


def _string_literals(path):
    """String constants that can reach a person: everything except docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def test_templates_and_scripts_are_english():
    files = [*APP.glob("templates/**/*.html"), *APP.glob("static/js/*.js")]
    assert _offenders(files, _lines) == []


def test_route_messages_are_english():
    assert _offenders(APP.glob("routers/*.py"), _string_literals) == []
