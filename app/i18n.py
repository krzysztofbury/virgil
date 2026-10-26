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


def _load() -> tuple[gettext.NullTranslations, dict[str, str | list[str]]]:
    with catalog_path().open("rb") as handle:
        catalog = read_po(handle)
    buffer = BytesIO()
    write_mo(buffer, catalog)
    buffer.seek(0)
    js: dict[str, str | list[str]] = {}
    for message in catalog:
        if message.id and message.string and any(name.startswith(_JS_SOURCE) for name, _line in message.locations):
            if isinstance(message.id, str):
                js[message.id] = message.string
            else:
                js[message.id[0]] = list(message.string)
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
    # A count can reach a template as text ("2" from a form or a joined row);
    # the Polish plural rule needs a number where English never looked.
    try:
        n = int(float(n))
    except TypeError, ValueError:
        n = 0
    return _translations().ngettext(singular, plural, n)


def N_(message: str) -> str:  # noqa: N802 - the conventional gettext marker name
    """Mark a module-level constant for extraction; translate it with _() where shown."""
    return message


WEEKDAYS_SHORT = (N_("Mon"), N_("Tue"), N_("Wed"), N_("Thu"), N_("Fri"), N_("Sat"), N_("Sun"))
WEEKDAYS = (N_("Monday"), N_("Tuesday"), N_("Wednesday"), N_("Thursday"), N_("Friday"), N_("Saturday"), N_("Sunday"))
MONTHS_SHORT = (
    N_("Jan"),
    N_("Feb"),
    N_("Mar"),
    N_("Apr"),
    N_("May"),
    N_("Jun"),
    N_("Jul"),
    N_("Aug"),
    N_("Sep"),
    N_("Oct"),
    N_("Nov"),
    N_("Dec"),
)


# Stored enumeration values a page shows as words: statuses, periods, horizons.
_ENUM_LABELS = {
    "active": N_("Active"),
    "paused": N_("Paused"),
    "completed": N_("Completed"),
    "abandoned": N_("Abandoned"),
    "pending": N_("Pending"),
    "carried": N_("Carried"),
    "skipped": N_("Skipped"),
    "day": N_("Day"),
    "week": N_("Week"),
    "month": N_("Month"),
    "quarter": N_("Quarter"),
    "year": N_("Year"),
    "male": N_("Male"),
    "female": N_("Female"),
    "admin": N_("Admin"),
    "user": N_("User"),
    "Warmup": N_("Warmup"),
    "Core": N_("Core"),
    "Cardio": N_("Cardio"),
    "Stretching": N_("Stretching"),
    "cash": N_("Cash"),
    "bonds": N_("Bonds"),
    "retirement": N_("Retirement"),
    "brokerage": N_("Brokerage"),
    "crypto": N_("Crypto"),
    "property": N_("Property"),
    "other": N_("Other"),
    "1yr": N_("1 Year"),
    "3yr": N_("3 Years"),
    "10yr": N_("10 Years"),
}


def enum_label(value) -> str:
    """A stored value as a word: translated when known, capitalized as before when not."""
    text = str(value)
    return _(_ENUM_LABELS[text]) if text in _ENUM_LABELS else text.capitalize()


def weekday_short(index: int) -> str:
    """Monday is 0, as date.weekday() counts."""
    return _(WEEKDAYS_SHORT[index])


def weekday_name(index: int) -> str:
    return _(WEEKDAYS[index])


def month_short(month: int) -> str:
    """January is 1, as date.month counts."""
    return _(MONTHS_SHORT[month - 1])


class LocalizedError(ValueError):
    """A service error the API reports in English and a page in the reader's language.

    str() is the English API message, unchanged. localized() is for pages: the
    template goes through the catalog, and parameters named in `labels` (field
    names such as "weight") are shown as their translated label.
    """

    labels: dict[str, str] = {}

    def __init__(self, message: str, **params) -> None:
        self.template, self.params = message, params
        # Without params the message is final text, braces and all.
        super().__init__(message.format(**params) if params else message)

    def localized(self) -> str:
        if current_language() == DEFAULT_LANGUAGE:
            return str(self)
        if not self.params:
            return _(self.template)
        # Only field names are translated; a user's own text is shown as given.
        params = {
            key: _(self.labels[value]) if isinstance(value, str) and value in self.labels else value
            for key, value in self.params.items()
        }
        return _(self.template).format(**params)


class LocalizedLookupError(LocalizedError, LookupError):
    """A missing record, for code that catches LookupError, worded like LocalizedError."""


def localized(exc: Exception) -> str:
    """An exception's message for a page: translated when the exception supports it."""
    return exc.localized() if isinstance(exc, LocalizedError) else str(exc)


def js_catalog(language: str) -> dict[str, str | list[str]]:
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
