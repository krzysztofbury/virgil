#!/usr/bin/env python3
"""Write the hosted instance's SSH and inventory inputs from 1Password (#174).

The repository is public, so the host address, port, hostname and keys live in
the password manager. This reads fixed references, refuses anything malformed
before writing, proves the operator key pair matches, and installs each output
owner-only. The outputs are ignored by Git (see .gitignore).

Adapted from the Minos materializer, without its multi-file transaction: every
value is validated before the first write, and each file is replaced
atomically on its own.
"""

import base64
import binascii
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path

DEPLOYMENT_ITEM = "op://Virgil/virgil-production-deployment"
SSH_ITEM = "op://Virgil/virgil-mikrus-ssh"
REFERENCES = {
    "ansible_host": f"{DEPLOYMENT_ITEM}/ansible_host",
    "ansible_port": f"{DEPLOYMENT_ITEM}/ansible_port",
    "ansible_user": f"{DEPLOYMENT_ITEM}/ansible_user",
    "expected_hostname": f"{DEPLOYMENT_ITEM}/expected_hostname",
    "ssh_host_key": f"{DEPLOYMENT_ITEM}/ssh_host_ed25519_public_key",
    "operator_public_key": f"{SSH_ITEM}/public key",
    "operator_private_key": f"{SSH_ITEM}/private key?ssh-format=openssh",
}
PRIVATE_KEY_NAME = "virgil_mikrus_ed25519"  # pragma: allowlist secret
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_BASE64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_ED25519_PREFIX = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"
_PRIVATE_KEY_BEGIN = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
_PRIVATE_KEY_END = "-----END OPENSSH " + "PRIVATE KEY-----"


class MaterializationError(RuntimeError):
    pass


def materialize(
    repository: Path,
    home: Path,
    *,
    read_secret: Callable[[str], str] | None = None,
    derive_public_key: Callable[[Path], str] | None = None,
) -> None:
    reader = read_secret or _read_secret
    values: dict[str, str] = {}
    for name, reference in REFERENCES.items():
        try:
            values[name] = reader(reference)
        except KeyError, MaterializationError:
            raise MaterializationError(f"1Password read failed for {name}") from None
        if "{{" in values[name] or "op://" in values[name]:
            raise MaterializationError(f"{name} is unresolved")

    host = _host(values["ansible_host"])
    port = _port(values["ansible_port"])
    if values["ansible_user"] != "root":
        raise MaterializationError("ansible_user must equal root")
    hostname = values["expected_hostname"]
    if _HOST_LABEL.fullmatch(hostname) is None:
        raise MaterializationError("expected_hostname is invalid")
    host_key = _ed25519_public_key(values["ssh_host_key"], "host key")
    operator_key = _ed25519_public_key(values["operator_public_key"], "operator public key")
    private_key = _private_key(values["operator_private_key"])

    private_path = home / ".ssh" / PRIVATE_KEY_NAME
    _require_key_pair(private_path.parent, private_key, operator_key, derive_public_key or _derive_public_key)

    ansible = repository / "infra/ansible"
    host_vars = (
        "---\n"
        f'ansible_host: "{host}"\n'
        f"ansible_port: {port}\n"
        'ansible_user: "root"\n'
        f'expected_hostname: "{hostname}"\n'
        f'ssh_exposure: "{ssh_exposure(host, port)}"\n'
    )
    # OpenSSH records and looks up port 22 under the bare host name.
    known_hosts = f"{host} {host_key}\n" if port == 22 else f"[{host}]:{port} {host_key}\n"
    outputs = {
        private_path: private_key,
        ansible / "inventory/host_vars/virgil-production.yml": host_vars,
        ansible / "known_hosts": known_hosts,
        ansible / "files/root_authorized_keys": f"{operator_key}\n",
    }
    for path, content in outputs.items():
        _install(path, content.encode())


def ssh_exposure(host: str, port: int) -> str:
    """A tailnet name on port 22 means public SSH is closed; anything else is the provider's mapping."""
    return "tailnet" if host.endswith(".ts.net") and port == 22 else "public"


def _read_secret(reference: str) -> str:
    try:
        completed = subprocess.run(
            ["op", "read", "--no-newline", reference], check=False, capture_output=True, text=True, timeout=30
        )
    except OSError, subprocess.TimeoutExpired:
        raise MaterializationError("1Password read failed") from None
    if completed.returncode != 0:
        raise MaterializationError("1Password read failed")
    return completed.stdout


def _derive_public_key(private_path: Path) -> str:
    try:
        completed = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(private_path)], check=False, capture_output=True, text=True, timeout=10
        )
    except OSError, subprocess.TimeoutExpired:
        raise MaterializationError("operator private key validation failed") from None
    if completed.returncode != 0:
        raise MaterializationError("operator private key validation failed")
    return completed.stdout


def _host(value: str) -> str:
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(_HOST_LABEL.fullmatch(label) is None for label in labels):
        raise MaterializationError("ansible_host is invalid")
    return value


def _port(value: str) -> int:
    if re.fullmatch(r"[0-9]{1,5}", value) is None or not 1 <= int(value) <= 65535:
        raise MaterializationError("ansible_port is invalid")
    return int(value)


def _ed25519_public_key(value: str, name: str) -> str:
    parts = value.strip().split()
    if len(value) > 1024 or len(value.strip().splitlines()) != 1 or len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise MaterializationError(f"{name} is invalid")
    if _BASE64.fullmatch(parts[1]) is None:
        raise MaterializationError(f"{name} is invalid")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except binascii.Error, ValueError:
        raise MaterializationError(f"{name} is invalid") from None
    if len(blob) != 51 or not blob.startswith(_ED25519_PREFIX):
        raise MaterializationError(f"{name} is invalid")
    return f"ssh-ed25519 {parts[1]}"


def _private_key(value: str) -> str:
    lines = value.strip().splitlines()
    if (
        len(value) > 16_384
        or len(lines) < 3
        or lines[0] != _PRIVATE_KEY_BEGIN
        or lines[-1] != _PRIVATE_KEY_END
        or any(_BASE64.fullmatch(line) is None for line in lines[1:-1])
    ):
        raise MaterializationError("operator private key is invalid")
    return "\n".join(lines) + "\n"


def _require_key_pair(ssh_dir: Path, private_key: str, expected_public_key: str, derive: Callable[[Path], str]) -> None:
    if ssh_dir.is_symlink():
        raise MaterializationError("refusing to use a symbolic-link SSH directory")
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    candidate = ssh_dir / f".{PRIVATE_KEY_NAME}.{uuid.uuid4().hex}.validate"
    try:
        _write_new(candidate, private_key.encode())
        derived = _ed25519_public_key(derive(candidate), "derived public key")
    finally:
        candidate.unlink(missing_ok=True)
    if derived != expected_public_key:
        raise MaterializationError("operator public and private keys do not match")


def _install(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or (destination.exists() and not destination.is_file()):
        raise MaterializationError(f"refusing to replace a non-regular file: {destination.name}")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        _write_new(temporary, content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _write_new(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    try:
        materialize(repository, Path.home())
    except MaterializationError as error:
        print(f"host materialization failed: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("host materialization failed: local operation failed", file=sys.stderr)
        return 1
    print("Materialized the Virgil host inputs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
