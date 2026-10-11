"""Refuse a release alias unless the version matches the package and changelog."""

import re
import sys
import tomllib
from pathlib import Path


def validate(tag: str, root: Path) -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", tag):
        raise ValueError("Release tags must be vMAJOR.MINOR.PATCH")
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    if tag != f"v{project['version']}":
        raise ValueError("Release tag does not match project.version")
    if not re.search(
        rf"^## \[{re.escape(project['version'])}\] - \d{{4}}-\d{{2}}-\d{{2}}$",
        (root / "CHANGELOG.md").read_text(),
        re.M,
    ):
        raise ValueError("Release requires a dated changelog entry")


if __name__ == "__main__":
    validate(sys.argv[1], Path(__file__).resolve().parents[1])
