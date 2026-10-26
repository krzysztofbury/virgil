"""Outgoing email (#168): the only code that sends mail.

Everything else renders a message with `render_mail` and hands it to
`schedule`, which sends after the response, so a slow or failing mail server
never delays a page or reveals, by its timing, whether an address has an
account. The backend is configuration: "smtp" sends through any SMTP server
with STARTTLS (moving provider is a settings change), "log" writes the message
to the log for development, and "" sends nothing.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from app import config
from app.i18n import reset_language, set_language

logger = logging.getLogger(__name__)

_pending: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    html: str
    reply_to: str = ""


def mail_enabled() -> bool:
    if config.MAIL_BACKEND == "log":
        return True
    return config.MAIL_BACKEND == "smtp" and bool(config.SMTP_HOST and config.MAIL_FROM)


def render_mail(template: str, language: str, *, to: str, **context) -> Mail:
    """Render email/<template>.txt and .html in the recipient's language.

    The text template's first line is "Subject: ...".
    """
    from app.main import templates

    token = set_language(language)
    try:
        text = templates.env.get_template(f"email/{template}.txt").render(**context)
        html = templates.env.get_template(f"email/{template}.html").render(**context)
    finally:
        reset_language(token)
    first, _newline, body = text.lstrip().partition("\n")
    if not first.startswith("Subject:"):
        raise ValueError(f"email/{template}.txt must start with a Subject: line")
    return Mail(
        to=to,
        subject=first.removeprefix("Subject:").strip(),
        text=body.lstrip("\n"),
        html=html,
        reply_to=config.MAIL_REPLY_TO,
    )


def _message(mail: Mail) -> EmailMessage:
    message = EmailMessage()
    message["From"] = config.MAIL_FROM
    message["To"] = mail.to
    message["Subject"] = mail.subject
    message["Date"] = formatdate(localtime=True)
    domain = parseaddr(config.MAIL_FROM)[1].rpartition("@")[2] or None
    message["Message-ID"] = make_msgid(domain=domain)
    if mail.reply_to:
        message["Reply-To"] = mail.reply_to
    message.set_content(mail.text)
    message.add_alternative(mail.html, subtype="html")
    return message


def _send_smtp(message: EmailMessage) -> None:
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        if config.SMTP_USERNAME:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(message)


async def send_now(mail: Mail) -> None:
    """Send one message with the configured backend. Raises on failure."""
    if config.MAIL_BACKEND == "log":
        # Development only, chosen by name: the message is logged whole so its
        # link can be followed. app startup warns when production uses this.
        logger.info("Mail to %s: %s\n%s", mail.to, mail.subject, mail.text)
        return
    if not mail_enabled():
        return
    await asyncio.to_thread(_send_smtp, _message(mail))


async def _send_logged(mail: Mail) -> None:
    try:
        await send_now(mail)
    except Exception as exc:  # noqa: BLE001 - a failed send is logged, never raised into a page
        # The class only: the message and its secret link stay out of the log.
        logger.warning("Mail to %s failed: %s", mail.to, type(exc).__name__)


def schedule(mail: Mail) -> None:
    """Send after the current request, without waiting and without raising."""
    task = asyncio.get_running_loop().create_task(_send_logged(mail))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for every message scheduled on this event loop (tests and shutdown).

    Tasks of another loop cannot be awaited here; they finish, or die, with it.
    """
    loop = asyncio.get_running_loop()
    while mine := [task for task in _pending if task.get_loop() is loop]:
        await asyncio.gather(*mine, return_exceptions=True)
