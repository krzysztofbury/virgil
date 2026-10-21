"""Third-party scripts, styles and fonts are served from /static/vendor, as recorded."""

import base64
import hashlib
import json
import re
from pathlib import Path

VENDOR = Path("app/static/vendor")
TEMPLATES = Path("app/templates")


def test_every_vendored_file_matches_its_recorded_hash():
    """A changed byte in a vendored file is a supply-chain change; it must be deliberate."""
    recorded = json.loads((VENDOR / "SOURCES.json").read_text())["files"]
    on_disk = {
        str(path.relative_to(VENDOR))
        for path in VENDOR.rglob("*")
        if path.is_file() and path.name not in {"SOURCES.json", "fonts.css"}
    }
    assert on_disk == set(recorded), "every vendored file is recorded, and nothing recorded is missing"
    for name, entry in recorded.items():
        digest = base64.b64encode(hashlib.sha384((VENDOR / name).read_bytes()).digest()).decode()
        assert entry["sha384"] == f"sha384-{digest}", f"{name} differs from the recorded download"


def test_no_template_loads_anything_from_another_origin():
    outside = re.compile(r"""(?:src|href)=["']https?://""")
    offenders = [
        f"{path.name}: {line.strip()[:80]}"
        for path in sorted(TEMPLATES.rglob("*.html"))
        for line in path.read_text().splitlines()
        if outside.search(line) and ("<script" in line or "stylesheet" in line or "preconnect" in line)
    ]
    assert offenders == []
