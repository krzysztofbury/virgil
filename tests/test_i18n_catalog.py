"""The Polish catalog (#152): complete, current with the source, and safe to show."""

import gettext
import re
import subprocess
import sys
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

from app.feedback import MAX_FEEDBACK_LENGTH
from app.i18n import catalog_path, normalize_language

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"%\(\w+\)s|\{\w*\}")


def _catalog():
    with catalog_path().open("rb") as handle:
        return read_po(handle)


def _messages():
    return [message for message in _catalog() if message.id]


def _forms(value) -> list[str]:
    return list(value) if isinstance(value, tuple) else [value]


def test_every_message_has_a_polish_translation():
    missing = [message.id for message in _messages() if not all(_forms(message.string))]
    fuzzy = [message.id for message in _messages() if message.fuzzy]
    assert missing == [], "run scripts/i18n.py update, then translate the new entries"
    assert fuzzy == []


def test_catalog_matches_the_source(tmp_path):
    pot = tmp_path / "messages.pot"
    subprocess.run(
        [sys.executable, "-m", "babel.messages.frontend", "-q", "extract", "-F", "babel.cfg", "-o", str(pot), "app"],
        cwd=ROOT,
        check=True,
    )
    with pot.open("rb") as handle:
        source = {message.id for message in read_po(handle) if message.id}
    assert source == {message.id for message in _messages()}, "run scripts/i18n.py update"


def test_placeholders_match():
    mismatched = []
    for message in _messages():
        expected = {name for form in _forms(message.id) for name in PLACEHOLDER.findall(form)}
        if any(set(PLACEHOLDER.findall(form)) != expected for form in _forms(message.string)):
            mismatched.append(_forms(message.id)[0])
    assert mismatched == []


def test_polish_fits_the_feedback_bound():
    too_long = [
        message.id
        for message in _messages()
        for form in _forms(message.string)
        if len(form) > MAX_FEEDBACK_LENGTH >= len(_forms(message.id)[0])
    ]
    assert too_long == []


def test_polish_plural_forms():
    catalog = _catalog()
    assert catalog.num_plurals == 3
    plural = gettext.c2py(catalog.plural_expr)
    assert [plural(n) for n in (1, 2, 4, 5, 12, 22, 25, 112)] == [0, 1, 1, 2, 2, 1, 2, 2]


@pytest.mark.parametrize("value", [None, "", "de", "PL", "x" * 5000])
def test_unknown_language_is_english(value):
    assert normalize_language(value) == "en"


def test_polish_is_polish():
    assert normalize_language("pl") == "pl"


def test_weekday_and_month_names_follow_the_language():
    from app.i18n import month_short, reset_language, set_language, weekday_name, weekday_short

    assert (weekday_short(0), weekday_name(6), month_short(5)) == ("Mon", "Sunday", "May")
    token = set_language("pl")
    try:
        assert (weekday_short(0), weekday_name(6), month_short(5)) == ("Pon", "Niedziela", "Maj")
    finally:
        reset_language(token)
