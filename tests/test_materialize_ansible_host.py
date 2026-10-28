"""Host metadata reaches Ansible only through ignored, owner-only files (#174).

The repository is public, so the address, port and keys live in the password
manager. The materializer must refuse anything malformed before it writes a
single file, because Ansible trusts the pinned host key it produces.
"""

import stat
import subprocess
from pathlib import Path

import pytest

from scripts.materialize_ansible_host import (
    DEPLOYMENT_ITEM,
    SSH_ITEM,
    MaterializationError,
    materialize,
)

_HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f"


@pytest.fixture
def operator_key(tmp_path: Path) -> tuple[str, str]:
    key = tmp_path / "operator"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    public = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    return key.read_text(), public


def _secrets(operator_key: tuple[str, str], **overrides: str) -> dict[str, str]:
    private, public = operator_key
    values = {
        f"{DEPLOYMENT_ITEM}/ansible_host": "srv1.mikr.us",
        f"{DEPLOYMENT_ITEM}/ansible_port": "10123",
        f"{DEPLOYMENT_ITEM}/ansible_user": "root",
        f"{DEPLOYMENT_ITEM}/expected_hostname": "virgil1",
        f"{DEPLOYMENT_ITEM}/ssh_host_ed25519_public_key": f"{_HOST_KEY} root@virgil1",
        f"{SSH_ITEM}/public key": public,
        f"{SSH_ITEM}/private key?ssh-format=openssh": private,
    }
    values.update({f"{DEPLOYMENT_ITEM}/{name}": value for name, value in overrides.items()})
    return values


def _outputs(repository: Path, home: Path) -> list[Path]:
    return [
        home / ".ssh/virgil_mikrus_ed25519",
        repository / "infra/ansible/inventory/host_vars/virgil-production.yml",
        repository / "infra/ansible/known_hosts",
        repository / "infra/ansible/files/root_authorized_keys",
    ]


def test_public_host_is_written_owner_only_with_the_port_pinned(tmp_path, operator_key):
    repository, home = tmp_path / "repo", tmp_path / "home"

    materialize(repository, home, read_secret=_secrets(operator_key).__getitem__)

    for path in _outputs(repository, home):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    host_vars = (repository / "infra/ansible/inventory/host_vars/virgil-production.yml").read_text()
    assert 'ansible_host: "srv1.mikr.us"' in host_vars
    assert "ansible_port: 10123" in host_vars
    assert 'ssh_exposure: "public"' in host_vars
    assert (repository / "infra/ansible/known_hosts").read_text() == f"[srv1.mikr.us]:10123 {_HOST_KEY}\n"
    assert (repository / "infra/ansible/files/root_authorized_keys").read_text() == f"{operator_key[1]}\n"


def test_tailnet_name_on_port_22_means_public_ssh_is_closed(tmp_path, operator_key):
    repository, home = tmp_path / "repo", tmp_path / "home"
    secrets = _secrets(operator_key, ansible_host="virgil-production.tail1234.ts.net", ansible_port="22")

    materialize(repository, home, read_secret=secrets.__getitem__)

    host_vars = (repository / "infra/ansible/inventory/host_vars/virgil-production.yml").read_text()
    assert 'ssh_exposure: "tailnet"' in host_vars
    # OpenSSH records port 22 under the bare name.
    assert (repository / "infra/ansible/known_hosts").read_text() == (
        f"virgil-production.tail1234.ts.net {_HOST_KEY}\n"
    )


@pytest.mark.parametrize(
    "override",
    [
        {"ansible_user": "admin"},
        {"ansible_port": "0"},
        {"ansible_host": "localhost"},
        {"ansible_host": "{{ op://Virgil/x }}"},
        {"expected_hostname": "Virgil_1"},
        {"ssh_host_ed25519_public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ"},
    ],
    ids=["non-root", "port", "single-label-host", "unresolved", "hostname", "rsa-host-key"],
)
def test_malformed_metadata_writes_nothing(tmp_path, operator_key, override):
    repository, home = tmp_path / "repo", tmp_path / "home"

    with pytest.raises(MaterializationError):
        materialize(repository, home, read_secret=_secrets(operator_key, **override).__getitem__)

    assert not any(path.exists() for path in _outputs(repository, home))


def test_a_private_key_that_does_not_match_the_public_key_is_refused(tmp_path, operator_key):
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    secrets = _secrets(operator_key)
    secrets[f"{SSH_ITEM}/private key?ssh-format=openssh"] = other.read_text()
    repository, home = tmp_path / "repo", tmp_path / "home"

    with pytest.raises(MaterializationError, match="do not match"):
        materialize(repository, home, read_secret=secrets.__getitem__)

    assert not any(path.exists() for path in _outputs(repository, home))
