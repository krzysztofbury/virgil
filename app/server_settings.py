"""Server settings an admin changes in the app (#179).

Each setting here also has an environment variable (VIRGIL_<name>, read by
app/config.py). A value saved in the app overrides it until the admin resets
the setting; the environment value then applies again. Readers use
`current.<NAME>`, which looks the override up on every access, so a change
applies on the next request or job without a restart. The app runs one
process (app/__main__.py), so the overrides are cached in memory: loaded at
startup and updated by every save and reset.

Settings that would break a running instance (the encryption key, the base
URL, paths, proxies) or would let a hijacked admin session lower everyone's
protection (the break-glass admin list, required two-factor sign-in) are not
here and stay environment-only.
"""

from dataclasses import dataclass
from email.utils import parseaddr
from urllib.parse import urlsplit

from app import config
from app.i18n import N_
from app.validation import valid_email


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    attribute: str
    group: str
    kind: str  # text, int, bool, email, mailbox, https, recipient, choice
    label: str
    secret: bool = False
    choices: tuple[str, ...] = ()

    @property
    def env_name(self) -> str:
        return f"VIRGIL_{self.attribute}"


GROUPS = {
    "email": N_("Email"),
    "alerts": N_("Alerts"),
    "offsite": N_("Off-site copies"),
    "instance": N_("Instance"),
    "api": N_("API access"),
    "operator": N_("Operator"),
}

SETTINGS = (
    Setting("MAIL_BACKEND", "email", "choice", N_("Sending"), choices=("smtp", "")),
    Setting("SMTP_HOST", "email", "text", N_("SMTP host")),
    Setting("SMTP_PORT", "email", "int", N_("SMTP port")),
    Setting("SMTP_USERNAME", "email", "text", N_("SMTP username")),
    Setting("SMTP_PASSWORD", "email", "text", N_("SMTP password"), secret=True),
    Setting("MAIL_FROM", "email", "mailbox", N_("Sender")),
    Setting("MAIL_REPLY_TO", "email", "email", N_("Reply-to address")),
    Setting("ALERT_WEBHOOK_URL", "alerts", "https", N_("Alert webhook URL"), secret=True),
    Setting("OFFSITE_ENDPOINT", "offsite", "https", N_("Endpoint")),
    Setting("OFFSITE_BUCKET", "offsite", "text", N_("Bucket")),
    Setting("OFFSITE_ACCESS_KEY_ID", "offsite", "text", N_("Access key ID")),
    Setting("OFFSITE_SECRET_ACCESS_KEY", "offsite", "text", N_("Secret access key"), secret=True),
    Setting("OFFSITE_RECIPIENT", "offsite", "recipient", N_("Encryption recipient (age public key)")),
    Setting("CONTACT_EMAIL", "instance", "email", N_("Contact address")),
    Setting("REGISTRATION_OPEN", "instance", "bool", N_("Anyone can create an account")),
    Setting("LANDING_PAGE", "instance", "bool", N_("Show the landing page to signed-out visitors")),
    Setting("PLAUSIBLE_SCRIPT_URL", "instance", "https", N_("Plausible script URL")),
    Setting("API_SENSITIVE", "api", "bool", N_("The server API key may read Freedom data")),
    Setting("API_FINANCE", "api", "bool", N_("The server API key may read finance data")),
    Setting("OPERATOR_NAME", "operator", "text", N_("Name or company")),
    Setting("OPERATOR_ADDRESS", "operator", "text", N_("Address")),
    Setting("OPERATOR_TAX_ID", "operator", "text", N_("Tax ID")),
)

_BY_ATTRIBUTE = {setting.attribute: setting for setting in SETTINGS}
_MAX_LENGTH = 2000

# attribute -> the saved text, decrypted. Absent means the environment applies.
_values: dict[str, str] = {}


def setting(attribute: str) -> Setting:
    try:
        return _BY_ATTRIBUTE[attribute]
    except KeyError:
        raise SettingError(N_("That setting cannot be changed in the app.")) from None


def _typed(item: Setting, raw: str):
    if item.kind == "int":
        return int(raw)
    if item.kind == "bool":
        return raw == "true"
    return raw


def _check(item: Setting, raw: str) -> str:
    """The text to store for raw, or SettingError. Empty clears text settings."""
    raw = raw.strip()
    if len(raw) > _MAX_LENGTH:
        raise SettingError(N_("That value is too long."))
    if item.kind == "bool":
        if raw not in ("true", "false"):
            raise SettingError(N_("Choose on or off."))
    elif item.kind == "int":
        if not raw.isdigit() or not 1 <= int(raw) <= 65535:
            raise SettingError(N_("Enter a port number from 1 to 65535."))
    elif item.kind == "choice":
        if raw not in item.choices:
            raise SettingError(N_("Choose one of the listed options."))
    elif not raw:
        return raw
    elif item.kind == "email":
        if not valid_email(raw):
            raise SettingError(N_("Enter an email address."))
    elif item.kind == "mailbox":
        if not valid_email(parseaddr(raw)[1]):
            raise SettingError(N_("Enter an address, optionally with a name: Virgil <no-reply@example.com>."))
    elif item.kind == "https":
        parts = urlsplit(raw)
        if parts.scheme != "https" or not parts.netloc:
            raise SettingError(N_("Enter an https:// address."))
    elif item.kind == "recipient":
        from app.recovery import parse_recipient

        try:
            parse_recipient(raw)
        except Exception:
            raise SettingError(N_("Enter an age public key, which starts with age1.")) from None
    return raw


class _Current:
    """`current.SMTP_HOST`: the app's value when one is saved, else app.config's."""

    def __getattr__(self, attribute: str):
        item = _BY_ATTRIBUTE.get(attribute)
        if item is not None and attribute in _values:
            return _typed(item, _values[attribute])
        return getattr(config, attribute)


current = _Current()


def source(attribute: str) -> str:
    """Where the value in use comes from: app, environment, or unset."""
    setting(attribute)
    if attribute in _values:
        return "app"
    return "environment" if getattr(config, attribute) not in ("", None, False) else "unset"


def masked(attribute: str) -> str:
    """A secret's last four characters, so the admin can tell which one is set."""
    value = str(getattr(current, attribute) or "")
    return f"...{value[-4:]}" if len(value) > 8 else ("****" if value else "")


async def load() -> None:
    """Read every saved value into memory. Called once the registry is migrated."""
    from app.central_db import get_central_db
    from app.services.encryption import decrypt

    db = await get_central_db()
    rows = await db.execute_fetchall("SELECT name, value FROM server_settings")
    values = {}
    for row in rows:
        item = _BY_ATTRIBUTE.get(row["name"])
        if item is not None:
            values[row["name"]] = decrypt(row["value"]) if item.secret else row["value"]
    _values.clear()
    _values.update(values)


async def save(attribute: str, raw: str, *, by: str = "") -> None:
    from app.central_db import get_central_db
    from app.services.encryption import encrypt

    item = setting(attribute)
    value = _check(item, raw)
    db = await get_central_db()
    await db.execute(
        """INSERT INTO server_settings (name, value, updated_by) VALUES (?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
           updated_at = datetime('now')""",
        (attribute, encrypt(value) if item.secret and value else value, by),
    )
    await db.commit()
    _values[attribute] = value


async def reset(attribute: str) -> None:
    from app.central_db import get_central_db

    setting(attribute)
    db = await get_central_db()
    await db.execute("DELETE FROM server_settings WHERE name = ?", (attribute,))
    await db.commit()
    _values.pop(attribute, None)
