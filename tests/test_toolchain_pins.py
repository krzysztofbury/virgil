"""The image ships the Python and uv that CI tests and a checkout pins.

Dependabot bumps only the Dockerfile (#205), so a patch release would
otherwise ship an interpreter the suite never ran on.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _match(pattern: str, path: str) -> str:
    found = re.search(pattern, (ROOT / path).read_text(), re.MULTILINE)
    assert found, f"{pattern!r} not found in {path}"
    return found.group(1)


def test_image_python_matches_ci_and_checkout():
    image = _match(r"^FROM python:([\d.]+)-slim@", "Dockerfile")
    ci = _match(r"^  PYTHON_VERSION: '([\d.]+)'$", ".github/workflows/ci.yml")
    checkout = (ROOT / ".python-version").read_text().strip()
    assert image == ci == checkout, (
        "Move every Python pin together: the Dockerfile FROM line, PYTHON_VERSION in "
        ".github/workflows/ci.yml, .python-version, and the version in CONTRIBUTING.md, "
        "README.md and SPEC.md. CI installs Python with UV_VERSION: if "
        "`uv python install <version>` fails there, move uv as well (test_image_uv_matches_ci). "
        "Then run `uv lock --check` and `uv lock --check --script mcp_server/virgil_mcp.py`."
    )


def test_image_uv_matches_ci():
    image = _match(r"^COPY --from=ghcr\.io/astral-sh/uv:([\d.]+)@", "Dockerfile")
    ci = _match(r"^  UV_VERSION: '([\d.]+)'$", ".github/workflows/ci.yml")
    assert image == ci, (
        "Move uv together: the Dockerfile COPY --from=ghcr.io/astral-sh/uv line (tag and digest), "
        "UV_VERSION in .github/workflows/ci.yml and the version in CONTRIBUTING.md."
    )
