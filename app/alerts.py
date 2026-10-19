"""One-line operator alerts posted to a chat webhook."""

import logging

import httpx

logger = logging.getLogger(__name__)


def degraded_start_alert(version: str, central_failed: bool, failed_accounts: int) -> str | None:
    """What to tell the operator after a start that left databases unmigrated, or None.

    Counts only: the alert leaves the machine, and account names do not need to.
    """
    if central_failed:
        problem = "the central database failed its migrations, so every page answers 503"
    elif failed_accounts:
        problem = f"{failed_accounts} account database(s) failed their migrations and answer errors"
    else:
        return None
    return (
        f"Virgil {version} started degraded: {problem}. /healthz answers 503. "
        "A restart will not fix this; the startup log names the cause, and RELEASE.md has the rollback."
    )


async def send_alert(url: str, text: str) -> bool:
    """Post {"text": text} to url. Failures are logged, never raised."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json={"text": text})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        # Not the exception text: it carries the URL, and webhook URLs hold tokens.
        logger.error("The alert webhook did not accept the alert (%s)", type(exc).__name__)
        return False
    return True
