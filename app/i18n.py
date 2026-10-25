"""English and Polish UI (#152).

English source text is the message id, so English needs no catalog and a missing
translation falls back to English. The Polish `.po` is compiled in memory once at
import; nothing is built into the image and no binary file is committed.
"""

import gettext
from contextvars import ContextVar, Token
from io import BytesIO
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

LANGUAGES = ("en", "pl")
DEFAULT_LANGUAGE = "en"
LANGUAGE_COOKIE = "virgil_lang"
_JS_SOURCE = "app/static/js/"

_language: ContextVar[str] = ContextVar("virgil_language", default=DEFAULT_LANGUAGE)


def catalog_path() -> Path:
    return Path(__file__).resolve().parent / "locale" / "pl" / "LC_MESSAGES" / "messages.po"


def _load() -> tuple[gettext.NullTranslations, dict[str, str]]:
    with catalog_path().open("rb") as handle:
        catalog = read_po(handle)
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    js = {
        message.id: message.string
        for message in catalog
        if message.id
        and isinstance(message.id, str)
        and message.string
        and any(filename.startswith(_JS_SOURCE) for filename, _line in message.locations)
    }
    return gettext.GNUTranslations(buffer), js


_POLISH, _POLISH_JS = _load()
_ENGLISH = gettext.NullTranslations()


def normalize_language(value: str | None) -> str:
    """Only exactly "pl" selects Polish; anything else, including garbage, is English."""
    return "pl" if value == "pl" else DEFAULT_LANGUAGE


def current_language() -> str:
    return _language.get()


def set_language(language: str) -> Token:
    return _language.set(normalize_language(language))


def reset_language(token: Token) -> None:
    _language.reset(token)


def _translations() -> gettext.NullTranslations:
    return _POLISH if _language.get() == "pl" else _ENGLISH


def _(message: str) -> str:
    return _translations().gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translations().ngettext(singular, plural, n)


def N_(message: str) -> str:  # noqa: N802 - the conventional gettext marker name
    """Mark a module-level constant for extraction; translate it with _() where shown."""
    return message


def js_catalog(language: str) -> dict[str, str]:
    """Polish strings our own scripts use; English needs none."""
    return dict(_POLISH_JS) if normalize_language(language) == "pl" else {}


class LanguageMiddleware:
    """Resolve each request's language: the account's choice, then the cookie, then English.

    Runs inside AuthMiddleware, so a signed-in request already carries its user row.
    Accept-Language is deliberately ignored: Polish appears only when chosen.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        user = scope.get("state", {}).get("user")
        chosen = user.get("language") if user else Request(scope).cookies.get(LANGUAGE_COOKIE)
        language = normalize_language(chosen)

        async def send_with_language(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Content-Language"] = language
            await send(message)

        token = set_language(language)
        try:
            await self.app(scope, receive, send_with_language)
        finally:
            reset_language(token)
