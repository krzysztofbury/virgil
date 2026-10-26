import ipaddress
import logging
import os
from pathlib import Path

# Load .env file if present (local development). In Docker, env_file handles this.
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

ENV = os.environ.get("VIRGIL_ENV", "local")  # "local" or "prod"
IS_PROD = ENV == "prod"

ENCRYPTION_KEY = os.environ.get("VIRGIL_ENCRYPTION_KEY", "")
BASE_URL = os.environ.get("VIRGIL_BASE_URL", "http://localhost:8123")
HOST = os.environ.get("VIRGIL_HOST", "0.0.0.0")
PORT = 8123

# Internal LLM — used for onboarding and system features.
# Fallback when no user-configured provider is active.
INTERNAL_LLM_MODEL = os.environ.get("VIRGIL_INTERNAL_LLM_MODEL", "gemini/gemini-3-flash-preview")
INTERNAL_LLM_KEY = os.environ.get("VIRGIL_INTERNAL_LLM_KEY", "")

# A user-initiated job that sits until the next scheduler tick reads as broken.
# Enqueueing wakes a bounded worker pass immediately; the tick stays the safety
# net. Tests turn this off so queue state stays deterministic.
WORKER_WAKE = os.environ.get("VIRGIL_WORKER_WAKE", "true").lower() == "true"

# Multi-user settings.
CENTRAL_DB_PATH = os.environ.get(
    "VIRGIL_CENTRAL_DB_PATH",
    str(Path(__file__).parent.parent / "data" / "virgil-central.db"),
)
USERS_DB_DIR = str(Path(CENTRAL_DB_PATH).parent / "users")
# Backups belong on another mount than the databases they protect, so losing or
# wiping the data volume does not take the copies with it. Empty keeps them in
# <data>/backups.
BACKUP_DIR_SETTING = os.environ.get("VIRGIL_BACKUP_DIR", "")
# Receives {"text": ...} when a start leaves a database unmigrated: /healthz is
# then red, and nothing else tells anyone. Empty sends nothing.
ALERT_WEBHOOK_URL = os.environ.get("VIRGIL_ALERT_WEBHOOK_URL", "")
# Off-site copies (app/services/offsite.py): an S3-compatible bucket such as
# Cloudflare R2, and the age public key every copy is encrypted to. All empty
# turns them off; a partial set is reported at startup and in Settings.
OFFSITE_ENDPOINT = os.environ.get("VIRGIL_OFFSITE_ENDPOINT", "").strip()
OFFSITE_BUCKET = os.environ.get("VIRGIL_OFFSITE_BUCKET", "").strip()
OFFSITE_ACCESS_KEY_ID = os.environ.get("VIRGIL_OFFSITE_ACCESS_KEY_ID", "").strip()
OFFSITE_SECRET_ACCESS_KEY = os.environ.get("VIRGIL_OFFSITE_SECRET_ACCESS_KEY", "").strip()
OFFSITE_RECIPIENT = os.environ.get("VIRGIL_OFFSITE_RECIPIENT", "").strip()
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("VIRGIL_ADMIN_EMAILS", "").split(",") if e.strip()]
# Closed by default: the documented deployment is internet-facing (Cloudflare
# Tunnel), and open registration would let anyone create accounts and burn
# disk/LLM resources. The FIRST account can always be created (bootstrap owner
# — see app/routers/auth.py registration_allowed()).
REGISTRATION_OPEN = os.environ.get("VIRGIL_REGISTRATION_OPEN", "false").lower() == "true"

# Every account with a password must also use an authenticator app (#168).
# Off by default; a hosted instance turns it on. Accounts without a password
# sign in with emailed links and are not affected.
REQUIRE_TOTP = os.environ.get("VIRGIL_REQUIRE_TOTP", "false").lower() == "true"

# Outgoing email (#168). "" sends nothing, "log" writes each message to the log
# for development, "smtp" sends through the server below. Sign-in links are
# only offered when mail can go out.
MAIL_BACKEND = os.environ.get("VIRGIL_MAIL_BACKEND", "").strip().lower()
SMTP_HOST = os.environ.get("VIRGIL_SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("VIRGIL_SMTP_PORT", "587") or 587)
SMTP_USERNAME = os.environ.get("VIRGIL_SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("VIRGIL_SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("VIRGIL_MAIL_FROM", "").strip()
MAIL_REPLY_TO = os.environ.get("VIRGIL_MAIL_REPLY_TO", "").strip()

# Trust Cloudflare's client-IP header only when the deployment guarantees that
# the tunnel is the sole ingress. Direct/local deployments keep the peer IP.
TRUST_CLOUDFLARE_HEADERS = os.environ.get("VIRGIL_TRUST_CLOUDFLARE_HEADERS", "false").lower() == "true"


def parse_trusted_proxies(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Addresses or CIDR ranges of the proxies allowed to send CF-Connecting-IP.

    A range covers a Docker network, where the tunnel container gets a new
    address on every recreate. An entry that is not an address or range is
    skipped with a warning: it can only ever trust less, never more.
    """
    networks = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            logging.getLogger(__name__).warning("VIRGIL_TRUSTED_PROXY_IPS: ignoring %r, not an IP or CIDR", entry)
            continue
        if network.prefixlen == 0:
            # Every peer could then claim any client address.
            logging.getLogger(__name__).warning("VIRGIL_TRUSTED_PROXY_IPS: ignoring %r, it trusts everyone", entry)
            continue
        networks.append(network)
    return tuple(networks)


TRUSTED_PROXY_IPS = parse_trusted_proxies(os.environ.get("VIRGIL_TRUSTED_PROXY_IPS", ""))

# Machine-to-machine REST API. Empty key = API disabled.
API_KEY = os.environ.get("VIRGIL_API_KEY", "")
API_USER_EMAIL = os.environ.get("VIRGIL_API_USER_EMAIL", "").strip().lower()
# /api/noporn returns intimate journal/relapse content — a leaked API key should
# not expose it by default. Opt in explicitly.
API_SENSITIVE = os.environ.get("VIRGIL_API_SENSITIVE", "false").lower() == "true"
# /api/finance returns balances and the safety cushion. Opt in separately, so
# finances over MCP do not also expose Freedom.
API_FINANCE = os.environ.get("VIRGIL_API_FINANCE", "false").lower() == "true"
