"""A hosted release pins one image digest for one full commit (#174).

Watchtower followed the mutable `latest` tag; the release script must refuse
anything that is not a digest-pinned image of this repository.
"""

import subprocess

import pytest

from scripts import deploy
from scripts.deploy import DeployError, release_env, release_evidence

_SHA = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret
_IMAGE = "ghcr.io/krzysztofbury/virgil@sha256:" + "a" * 64


def test_a_release_pins_the_digest_and_the_commit():
    assert release_env(_IMAGE, _SHA) == f"VIRGIL_IMAGE={_IMAGE}\nVIRGIL_REVISION={_SHA}\n"


@pytest.mark.parametrize(
    ("image", "revision"),
    [
        ("ghcr.io/krzysztofbury/virgil:latest", _SHA),
        ("ghcr.io/someone/virgil@sha256:" + "a" * 64, _SHA),
        ("ghcr.io/krzysztofbury/virgil@sha256:" + "a" * 63, _SHA),
        (_IMAGE, _SHA[:7]),
    ],
    ids=["mutable-tag", "other-repository", "short-digest", "short-sha"],
)
def test_anything_else_is_refused(image, revision):
    with pytest.raises(DeployError):
        release_env(image, revision)


# What the host prints for the evidence read, in the shapes Docker and df give
# them (`docker stats --format '{{.MemUsage}}'`, `df -P -h`), with the log
# line's prefix from app/main.py's logging format.
_PREVIOUS_SHA = "fedcba9876543210fedcba9876543210fedcba98"  # pragma: allowlist secret
_PREVIOUS_DIGEST = "sha256:" + "b" * 64
_STARTUP = "Virgil version=" + _SHA + " \u2014 central migration=ok, user migrations OK for 1 DB(s), 0 failed"
_HOST_OUTPUT = "\n".join(
    [
        f"previous_VIRGIL_IMAGE=ghcr.io/krzysztofbury/virgil@{_PREVIOUS_DIGEST}",
        f"previous_VIRGIL_REVISION={_PREVIOUS_SHA}",
        f"startup=2026-11-08 20:56:50,123 INFO app.main: {_STARTUP}",
        "memory=282.1MiB / 640MiB",
        "disk=4.4G of 9.8G (48%)",
        "",
    ]
)
_EVIDENCE = [
    "Evidence for the release record (docs/post-deploy-evidence.md):",
    f"  Image: {_IMAGE}",
    f"  Previous pin: {_PREVIOUS_DIGEST}, published as sha-{_PREVIOUS_SHA}",
    f"  Startup log: {_STARTUP}",
    "  Memory (used / mem_limit): 282.1MiB / 640MiB",
    "  Host disk (used of size): 4.4G of 9.8G (48%)",
]


def _ghcr(tags: dict[str, str]):
    def published(revision: str) -> str:
        if revision not in tags:
            raise DeployError(f"GHCR returned no digest for sha-{revision}")
        return tags[revision]

    return published


def test_the_release_evidence_names_every_host_field_of_the_record():
    lines = release_evidence(_HOST_OUTPUT, _IMAGE, _ghcr({_PREVIOUS_SHA: _PREVIOUS_DIGEST}))

    assert lines == _EVIDENCE


@pytest.mark.parametrize(
    ("host_output", "tags", "expected"),
    [
        (_HOST_OUTPUT, {}, f"{_PREVIOUS_DIGEST}, recorded for {_PREVIOUS_SHA}; GHCR could not be asked to confirm it"),
        (
            _HOST_OUTPUT,
            {_PREVIOUS_SHA: "sha256:" + "c" * 64},
            f"{_PREVIOUS_DIGEST}, recorded for {_PREVIOUS_SHA}, but GHCR's sha-{_PREVIOUS_SHA} is another digest",
        ),
        (
            _HOST_OUTPUT.replace(f"previous_VIRGIL_REVISION={_PREVIOUS_SHA}\n", ""),
            {_PREVIOUS_SHA: _PREVIOUS_DIGEST},
            f"{_PREVIOUS_DIGEST}, its commit is not recorded",
        ),
        (
            "\n".join(line for line in _HOST_OUTPUT.splitlines() if not line.startswith("previous_")),
            {},
            "none (no release.env.previous on the host)",
        ),
        (
            _HOST_OUTPUT.replace("ghcr.io/krzysztofbury/virgil@", "ghcr.io/krzysztofbury/virgil:latest#"),
            {_PREVIOUS_SHA: _PREVIOUS_DIGEST},
            "unavailable (release.env.previous holds no pinned ghcr.io/krzysztofbury/virgil image)",
        ),
    ],
    ids=["ghcr-unreachable", "ghcr-disagrees", "no-revision", "first-release", "unpinned"],
)
def test_the_previous_pin_says_how_far_its_commit_was_confirmed(host_output, tags, expected):
    lines = release_evidence(host_output, _IMAGE, _ghcr(tags))

    assert lines[2] == f"  Previous pin: {expected}"


def test_host_values_of_an_unexpected_shape_are_not_printed():
    """A failed or odd host read must not echo what the host said: env values, addresses, errors."""
    host_output = "\n".join(
        [
            "previous_VIRGIL_IMAGE=203.0.113.7",
            "VIRGIL_SECRET_KEY=s3cret-value",  # pragma: allowlist secret
            "startup=Error response from daemon: No such container: virgil",
            "memory=",
            "disk=df: /var/lib/virgil: No such file or directory",
        ]
    )

    output = "\n".join(release_evidence(host_output, _IMAGE, _ghcr({})))

    for leaked in ("203.0.113.7", "s3cret-value", "No such", "df:"):
        assert leaked not in output
    assert output.count("unavailable") == 4


def test_a_release_prints_the_evidence_after_the_public_check_from_one_host_read(monkeypatch, capsys):
    """One approved run gives the record its host fields; a failed read does not fail the done release."""
    host_commands = []

    def fake_ssh(command, *args, stdin=None):
        host_commands.append(command)
        if command == "sh -s":  # the evidence read
            return subprocess.CompletedProcess([], 0, stdout=_HOST_OUTPUT.encode(), stderr=b"")
        return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

    digests = {_SHA: _IMAGE.split("@", 1)[1], _PREVIOUS_SHA: _PREVIOUS_DIGEST}
    monkeypatch.setattr(deploy, "require_green_main", lambda revision: None)
    monkeypatch.setattr(deploy, "published_digest", lambda revision: digests[revision])
    monkeypatch.setattr(deploy, "wait_for_public_revision", lambda base_url, revision: None)
    monkeypatch.setattr(deploy, "ssh", fake_ssh)

    deploy.release(_SHA, "https://virgil.example")

    out = capsys.readouterr().out.splitlines()
    assert out[1] == f"https://virgil.example serves {_SHA}."
    assert out[2:] == _EVIDENCE
    assert host_commands.count("sh -s") == 1

    def failing_ssh(command, *args, stdin=None):
        if command == "sh -s":
            raise DeployError("host command failed (255): ssh: connect to host 203.0.113.7 port 22")
        return fake_ssh(command, *args, stdin=stdin)

    monkeypatch.setattr(deploy, "ssh", failing_ssh)
    deploy.release(_SHA, "https://virgil.example", public_check=False)

    captured = capsys.readouterr()
    assert "203.0.113.7" not in captured.out + captured.err
    assert captured.out.count("unavailable") == 3


def test_a_failed_public_check_still_prints_the_evidence_and_fails(monkeypatch, capsys):
    """The pin changed before the public check; its record needs the host fields from this run."""

    def fake_ssh(command, *args, stdin=None):
        stdout = _HOST_OUTPUT.encode() if command == "sh -s" else b""
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=b"")

    def public_check_fails(base_url, revision):
        raise DeployError(f"{base_url} does not serve {revision} yet")

    digests = {_SHA: _IMAGE.split("@", 1)[1], _PREVIOUS_SHA: _PREVIOUS_DIGEST}
    monkeypatch.setattr(deploy, "require_green_main", lambda revision: None)
    monkeypatch.setattr(deploy, "published_digest", lambda revision: digests[revision])
    monkeypatch.setattr(deploy, "wait_for_public_revision", public_check_fails)
    monkeypatch.setattr(deploy, "ssh", fake_ssh)

    with pytest.raises(DeployError, match="does not serve"):
        deploy.release(_SHA, "https://virgil.example")

    assert capsys.readouterr().out.splitlines()[1:] == _EVIDENCE
