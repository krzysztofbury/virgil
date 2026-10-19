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
ADMIN_EMAILS = [e.strip().lower() for e in os.environ.get("VIRGIL_ADMIN_EMAILS", "").split(",") if e.strip()]
# Closed by default: the documented deployment is internet-facing (Cloudflare
# Tunnel), and open registration would let anyone create accounts and burn
# disk/LLM resources. The FIRST account can always be created (bootstrap owner
# — see app/routers/auth.py registration_allowed()).
REGISTRATION_OPEN = os.environ.get("VIRGIL_REGISTRATION_OPEN", "false").lower() == "true"

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
