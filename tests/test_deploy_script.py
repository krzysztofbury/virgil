"""A hosted release pins one image digest for one full commit (#174).

Watchtower followed the mutable `latest` tag; the release script must refuse
anything that is not a digest-pinned image of this repository.
"""

import pytest

from scripts.deploy import DeployError, release_env

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
