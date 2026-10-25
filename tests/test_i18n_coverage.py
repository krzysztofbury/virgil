"""Polish coverage guards (#152): converted areas cannot regress to English.

PENDING_TEMPLATES and PENDING_MODULES list what is not converted yet. Each area's
conversion removes its entries; the lists end empty and are deleted with the
language selector, so no one can choose a half-translated Polish.
"""

import ast
import re
import sqlite3
from pathlib import Path

import pytest
from babel.messages.pofile import read_po
from conftest import _TMP, TEST_EMAIL

from app.i18n import catalog_path
from app.main import app

APP = Path(__file__).resolve().parents[1] / "app"
TEMPLATES = APP / "templates"

PENDING_TEMPLATES = {
    "admin_users.html",
    "auth_login.html",
    "auth_mfa_setup.html",
    "auth_mfa_verify.html",
    "auth_reset.html",
    "auth_signup.html",
    "base.html",
    "bloodwork.html",
    "daily.html",
    "dashboard.html",
    "experiment_detail.html",
    "experiment_edit.html",
    "experiment_new.html",
    "experiments.html",
    "finance.html",
    "finance_setup.html",
    "freedom.html",
    "goals.html",
    "offline.html",
    "onboarding.html",
    "oura.html",
    "partials/experiment_card.html",
    "partials/feedback.html",
    "partials/freedom_today.html",
    "partials/head_assets.html",
    "partials/job_notifications.html",
    "partials/job_status.html",
    "partials/oura_freshness.html",
    "partials/range_picker.html",
    "partials/today_block.html",
    "partials/wod_row.html",
    "settings.html",
    "settings_import.html",
    "settings_token_created.html",
    "training.html",
    "wod_confirm.html",
}
PENDING_MODULES = {
    "routers/admin.py",
    "routers/auth.py",
    "routers/bloodwork.py",
    "routers/daily.py",
    "routers/dashboard.py",
    "routers/experiments.py",
    "routers/finance.py",
    "routers/freedom.py",
    "routers/goals.py",
    "routers/jobs.py",
    "routers/life_scores.py",
    "routers/onboarding.py",
    "routers/oura.py",
    "routers/settings.py",
    "routers/training.py",
    "library_validation.py",
    "validation.py",
}

# Words that are the same in both languages: product names and units.
SAME_IN_BOTH = re.compile(r"\b(Virgil|A\.N\.D\.Y\.|Oura|WOD|MCP|API|AI|OK|kg|cm|bpm|ms|h|min|kcal|PLN)\b")
WORD = re.compile(r"[^\W\d_]{2,}")
_JINJA_TRANS = re.compile(r"{%-?\s*trans\b.*?{%-?\s*endtrans\s*-?%}", re.S)
_JINJA = re.compile(r"{{.*?}}|{%.*?%}|{#.*?#}", re.S)
_HIDDEN = re.compile(r"<script\b.*?</script>|<style\b.*?</style>|<!--.*?-->", re.S | re.I)
_TEXT_ATTRIBUTES = re.compile(
    r'\b(?:placeholder|title|aria-label|alt|data-confirm|data-label|data-pending-label)="([^"]*)"'
)
_TEXT_NODE = re.compile(r">([^<>]+)<")
# A quoted literal inside a Jinja tag that reads like a sentence or a label.
_JINJA_LITERAL = re.compile(r"""(?<![\w(])(['"])([A-Z][a-z]+(?: [^'"]*)?|[a-z]+ [a-z][^'"]*)\1""")
_MARKED_CALL = re.compile(r"""\b(?:_|gettext|ngettext)\(\s*(['"]).*?\1""", re.S)


def _untranslated_template_text(source: str) -> list[str]:
    found = []
    tags = _JINJA.findall(_JINJA_TRANS.sub("", source))
    for tag in tags:
        unmarked = _MARKED_CALL.sub("", tag)
        found += [literal for _quote, literal in _JINJA_LITERAL.findall(unmarked)]
    markup = _HIDDEN.sub("", _JINJA.sub("", _JINJA_TRANS.sub("", source)))
    markup = re.sub(r"&\w+;|&#\d+;", " ", markup)
    for text in [*_TEXT_NODE.findall(markup), *_TEXT_ATTRIBUTES.findall(markup)]:
        if WORD.search(SAME_IN_BOTH.sub("", text)):
            found.append(" ".join(text.split()))
    return found


def test_templates_have_no_untranslated_text():
    offenders = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        name = path.relative_to(TEMPLATES).as_posix()
        if name in PENDING_TEMPLATES:
            continue
        found = _untranslated_template_text(path.read_text(encoding="utf-8"))
        if found:
            offenders[name] = found
    assert offenders == {}


FEEDBACK_CALLS = {"success_redirect", "error_redirect", "feedback_redirect", "feedback_url", "feedback_headers"}


def _is_marked(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) in {"_", "ngettext"} or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_marked(node.func.value)
    )


def _unmarked_messages(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) not in FEEDBACK_CALLS:
            continue
        candidates = [keyword.value for keyword in node.keywords if keyword.arg in {"msg", "err", "message"}]
        candidates += node.args[2:3] if node.func.id.endswith("_redirect") else node.args[:1]
        for value in candidates:
            if isinstance(value, ast.Constant | ast.JoinedStr) and not _is_marked(value):
                found.append(f"{path.relative_to(APP)}:{node.lineno}")
    return found


def test_messages_are_marked():
    modules = [*APP.glob("routers/*.py"), APP / "validation.py", APP / "library_validation.py"]
    offenders = []
    for path in modules:
        name = path.relative_to(APP).as_posix()
        if name in PENDING_MODULES or name in {"routers/api.py", "routers/__init__.py", "routers/oura_webhook.py"}:
            continue
        offenders += _unmarked_messages(path)
    assert offenders == []


def test_pending_lists_only_name_real_files():
    assert {name for name in PENDING_TEMPLATES if not (TEMPLATES / name).exists()} == set()
    assert {name for name in PENDING_MODULES if not (APP / name).exists()} == set()


def _translated_ids() -> list[str]:
    with catalog_path().open("rb") as handle:
        catalog = read_po(handle)
    return [
        message.id
        for message in catalog
        if isinstance(message.id, str) and len(message.id) >= 12 and message.string and message.string != message.id
    ]


def _visible_text(html: str) -> str:
    html = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", html, flags=re.S | re.I)
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _html_pages() -> list[str]:
    from fastapi.responses import HTMLResponse

    routes = []
    for route in app.routes:
        # Included routers are wrapped; their own routes carry the full paths.
        routes += getattr(getattr(route, "original_router", None), "routes", [route])
    paths = sorted(
        {
            route.path
            for route in routes
            if "GET" in (getattr(route, "methods", None) or set())
            and getattr(route, "response_class", None) is HTMLResponse
            and "{" not in route.path
        }
    )
    tabs = ["general", "configuration", "integrations", "data", "automation", "security"]
    return [path for path in paths if path != "/settings"] + [f"/settings?tab={tab}" for tab in tabs]


def _set_language(language: str) -> None:
    central = sqlite3.connect(Path(_TMP) / "central.db")
    central.execute("UPDATE users SET language = ? WHERE email = ?", (language, TEST_EMAIL))
    central.commit()
    central.close()


@pytest.fixture
def polish(auth_client):
    """A Polish account in a browser that signed in as it (sign-in sets the cookie,
    which is what public pages such as /signup go by)."""
    from app.i18n import LANGUAGE_COOKIE

    _set_language("pl")
    auth_client.cookies.set(LANGUAGE_COOKIE, "pl")
    try:
        yield auth_client
    finally:
        _set_language("en")
        auth_client.cookies.delete(LANGUAGE_COOKIE)


@pytest.fixture
def modules_on():
    """Freedom and Finance are behind module switches; render them too."""
    from conftest import user_db_path

    conn = sqlite3.connect(user_db_path())
    before = dict(conn.execute("SELECT key, value FROM app_settings WHERE key IN ('feature_freedom', 'feature_finance')"))
    conn.executemany(
        "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, '1')", [("feature_freedom",), ("feature_finance",)]
    )
    conn.commit()
    try:
        yield
    finally:
        conn.execute("DELETE FROM app_settings WHERE key IN ('feature_freedom', 'feature_finance')")
        conn.executemany("INSERT INTO app_settings (key, value) VALUES (?, ?)", list(before.items()))
        conn.commit()
        conn.close()


@pytest.mark.parametrize("path", _html_pages())
def test_polish_pages_render(polish, modules_on, path):
    response = polish.get(path, follow_redirects=False)
    if response.status_code in (302, 303, 307):
        pytest.skip(f"{path} redirects a signed-in account ({response.headers['location']})")
    assert response.status_code == 200
    # The service worker caches one offline page for everyone; it carries both languages.
    if path != "/offline":
        assert '<html lang="pl"' in response.text or "<html" not in response.text
    text = _visible_text(response.text)
    assert [message for message in _translated_ids() if message in text] == []


@pytest.mark.xfail(strict=True, reason="the daily log is converted with the dashboard area")
def test_htmx_partial_is_polish(polish):
    from conftest import csrf_token

    token = csrf_token(polish, "/daily")
    response = polish.post(
        "/daily/measurements",
        data={"date": "2026-07-05", "weight": "70", "_csrf_token": token},
        headers={"HX-Request": "true"},
    )
    from urllib.parse import unquote

    assert unquote(response.headers["x-feedback-message"]) == "Zapisano pomiary"


def test_api_errors_stay_english(polish):
    from app.central_db import create_api_token

    central = sqlite3.connect(Path(_TMP) / "central.db")
    user_id = central.execute("SELECT id FROM users WHERE email = ?", (TEST_EMAIL,)).fetchone()[0]
    central.close()
    import asyncio

    secret = asyncio.run(create_api_token(user_id, "i18n", ["read"], 1))
    try:
        response = polish.get("/api/daily/not-a-date", headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code in (400, 404, 422)
        detail = str(response.json()["detail"])
        assert not re.search(r"[ąćęłńóśźż]", detail), detail
    finally:
        central = sqlite3.connect(Path(_TMP) / "central.db")
        central.execute("DELETE FROM api_tokens WHERE name = 'i18n'")
        central.commit()
        central.close()


def test_feedback_header_carries_polish_safely():
    from urllib.parse import unquote

    from app.feedback import feedback_headers

    headers = feedback_headers("Zapisano zmiany: łódź, żółć", kind="success")
    headers["X-Feedback-Message"].encode("latin-1")
    assert unquote(headers["X-Feedback-Message"]) == "Zapisano zmiany: łódź, żółć"
    assert headers["X-Feedback-Kind"] == "success"


def test_js_catalog_is_embedded_safely(polish, monkeypatch):
    import json

    import app.i18n as i18n

    monkeypatch.setattr(i18n, "_POLISH_JS", {"Saved": "</script><b>Zapisano</b>"})
    page = polish.get("/daily").text
    block = re.search(r'<script type="application/json" id="i18n-catalog"[^>]*>(.*?)</script>', page, re.S)
    assert block, "the page carries the catalog for our scripts"
    assert json.loads(block.group(1)) == {"Saved": "</script><b>Zapisano</b>"}
