#!/usr/bin/env python3
"""Release the hosted instance by image digest over SSH (#174).

    uv run python scripts/deploy.py release [SHA]   # default: the head of remote main
    uv run python scripts/deploy.py env              # render /etc/virgil/*.env from 1Password

A release takes a commit on remote `main` whose CI run succeeded, resolves the
digest CI published as `sha-<SHA>`, installs infra/compose/compose.yaml, pins
that digest in /opt/virgil/release.env, and waits for the container to be
healthy. It then checks that the public site serves that commit. There is no
automatic rollback: migrations are one-way and an older image refuses a newer
database (RELEASE.md). The previous pin stays in release.env.previous.

The host address comes from the materialized, ignored host variables
(infra/ansible/README.md). Nothing here holds a secret.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "krzysztofbury/virgil"
IMAGE = f"ghcr.io/{REPOSITORY}"
HOST_VARS = ROOT / "infra/ansible/inventory/host_vars/virgil-production.yml"
KNOWN_HOSTS = ROOT / "infra/ansible/known_hosts"
COMPOSE = ROOT / "infra/compose/compose.yaml"
ENV_TEMPLATES = {
    "/etc/virgil/virgil.env": ROOT / "infra/compose/virgil.env.tpl",
    "/etc/virgil/cloudflared.env": ROOT / "infra/compose/cloudflared.env.tpl",
}
BASE_URL = "https://virgil.pl"
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)

# Runs on the host as root. One release at a time; the new pin replaces the
# old one only after the image is on disk.
_RELEASE_SCRIPT = r"""
set -eu
image="$1"
revision="$2"
exec 9>/run/virgil-deploy.lock
flock -n 9 || { echo "another release is running" >&2; exit 75; }
cd /opt/virgil
docker pull --quiet "$image" >/dev/null
[ -f release.env ] && cp -p release.env release.env.previous
umask 077
printf 'VIRGIL_IMAGE=%s\nVIRGIL_REVISION=%s\n' "$image" "$revision" > release.env.new
mv release.env.new release.env
docker compose --file compose.yaml --env-file release.env up --detach --remove-orphans --wait --wait-timeout 420
docker image prune --force >/dev/null
"""


class DeployError(RuntimeError):
    pass


def release_env(image: str, revision: str) -> str:
    """What the host's release.env holds for one release."""
    if not image.startswith(f"{IMAGE}@") or _DIGEST.fullmatch(image.split("@", 1)[1]) is None:
        raise DeployError(f"not a pinned {IMAGE} image: {image}")
    if _SHA.fullmatch(revision) is None:
        raise DeployError(f"not a full commit SHA: {revision}")
    return f"VIRGIL_IMAGE={image}\nVIRGIL_REVISION={revision}\n"


def serves_revision(html: str, revision: str) -> bool:
    """The page links its assets with the build's commit as the cache-buster."""
    return f"/static/css/app.css?v={revision}" in html


def host_target() -> tuple[str, str]:
    if not HOST_VARS.is_file() or not KNOWN_HOSTS.is_file():
        raise DeployError("run scripts/materialize_ansible_host.py first")
    values: dict[str, str] = {}
    for line in HOST_VARS.read_text().splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    if values.get("ansible_user") != "root" or not values.get("ansible_port", "").isdigit():
        raise DeployError("materialized host variables are invalid")
    return values["ansible_host"], values["ansible_port"]


def ssh(command: str, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    host, port = host_target()
    argv = [
        "ssh",
        *("-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"),
        *("-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}"),
        *("-o", "GlobalKnownHostsFile=/dev/null", "-o", "HostKeyAlgorithms=ssh-ed25519"),
        *("-o", "IdentitiesOnly=yes", "-o", "PreferredAuthentications=publickey"),
        *("-i", str(Path.home() / ".ssh/virgil_mikrus_ed25519"), "-p", port, f"root@{host}"),
        command,
        *args,
    ]
    completed = subprocess.run(argv, input=stdin, capture_output=True, timeout=900, check=False)
    if completed.returncode != 0:
        raise DeployError(f"host command failed ({completed.returncode}): {completed.stderr.decode().strip()}")
    return completed


def remote_main_sha() -> str:
    out = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/main"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return out.split()[0]


def require_green_main(revision: str) -> None:
    subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT, check=True)
    on_main = subprocess.run(["git", "merge-base", "--is-ancestor", revision, "origin/main"], cwd=ROOT, check=False)
    if on_main.returncode != 0:
        raise DeployError(f"{revision} is not on remote main")
    runs = json.loads(
        subprocess.run(
            [
                *("gh", "run", "list", "--repo", REPOSITORY, "--workflow", "ci.yml"),
                *("--commit", revision, "--branch", "main", "--event", "push"),
                *("--json", "databaseId,status,conclusion"),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    if not any(run["status"] == "completed" and run["conclusion"] == "success" for run in runs):
        raise DeployError(f"no successful main CI run for {revision}: {runs}")


def published_digest(revision: str) -> str:
    """The digest GHCR serves for the tag CI pushed for this commit (the package is public)."""
    token_url = f"https://ghcr.io/token?scope=repository:{REPOSITORY}:pull"
    with urllib.request.urlopen(token_url, timeout=20) as response:  # nosec B310: fixed https URL
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"https://ghcr.io/v2/{REPOSITORY}/manifests/sha-{revision}",
        method="HEAD",
        headers={"Authorization": f"Bearer {token}", "Accept": _MANIFEST_TYPES},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # nosec B310: fixed https URL
        digest = response.headers.get("Docker-Content-Digest", "")
    if _DIGEST.fullmatch(digest) is None:
        raise DeployError(f"GHCR returned no digest for sha-{revision}")
    return digest


def wait_for_public_revision(base_url: str, revision: str, attempts: int = 10) -> None:
    if not base_url.startswith("https://"):
        raise DeployError("the public URL must be https://")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(f"{base_url}/login", timeout=20) as response:  # nosec B310: https only
                if serves_revision(response.read().decode(), revision):
                    return
        except OSError:
            pass
        if attempt < attempts - 1:
            time.sleep(6)
    raise DeployError(f"{base_url} does not serve {revision} yet")


def release(revision: str | None, base_url: str) -> None:
    revision = revision or remote_main_sha()
    if _SHA.fullmatch(revision) is None:
        raise DeployError("give the full 40-character commit SHA")
    require_green_main(revision)
    image = f"{IMAGE}@{published_digest(revision)}"
    release_env(image, revision)
    print(f"Releasing {revision} as {image}")
    ssh("install -m 0600 -o root -g root /dev/stdin /opt/virgil/compose.yaml", stdin=COMPOSE.read_bytes())
    ssh("sh -s --", image, revision, stdin=_RELEASE_SCRIPT.encode())
    wait_for_public_revision(base_url, revision)
    print(f"{base_url} serves {revision}.")


def render_env() -> None:
    for destination, template in ENV_TEMPLATES.items():
        rendered = subprocess.run(
            ["op", "inject", "--in-file", str(template)], capture_output=True, check=False, timeout=60
        )
        if rendered.returncode != 0:
            raise DeployError(f"op inject failed for {template.name}")
        ssh(
            f"umask 077 && cat > {destination}.new && mv {destination}.new {destination}",
            stdin=rendered.stdout,
        )
        print(f"Wrote {destination}; it applies on the next release.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    release_parser = commands.add_parser("release", help="release a commit on main by digest")
    release_parser.add_argument("revision", nargs="?", help="full commit SHA; default the head of remote main")
    release_parser.add_argument("--base-url", default=BASE_URL)
    commands.add_parser("env", help="render the host's env files from 1Password")
    arguments = parser.parse_args()
    try:
        if arguments.command == "release":
            release(arguments.revision, arguments.base_url)
        else:
            render_env()
    except DeployError as error:
        print(f"deploy failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
