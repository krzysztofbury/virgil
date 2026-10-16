"""Regenerate the README screenshots from fictional demo data.

Seeds a throwaway instance with scripts/seed_demo.py in a temporary directory,
serves it on its own port, logs in as the demo user and writes desktop, dark-theme,
full-page screenshots to docs/screenshots/, stored as 256-colour PNGs. Nothing
touches data/ or a running dev server, and the temporary instance is removed
afterwards.

    uv run --with playwright --with pillow python scripts/screenshots.py

Playwright needs a Chromium: run `uv run --with playwright playwright install
chromium` once, or pass an installed one with --browser /usr/bin/chromium.
"""

import argparse
import http.client
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README_DIR = ROOT / "docs" / "screenshots"
# The README gallery, in order. EXTRA pages can be named to check a change.
PAGES = {
    "dashboard": "/",
    "daily": "/daily",
    "training": "/training",
    "oura": "/oura",
    "freedom": "/freedom",
    "experiments": "/experiments",
}
EXTRA = {"goals": "/goals", "blood": "/bloodwork", "settings": "/settings", "app-config": "/settings?tab=configuration"}


def _wait_until_up(port: int, server: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"The demo server exited with code {server.returncode}")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/healthz")
            if connection.getresponse().status == 200:
                return
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.5)
    raise SystemExit("The demo server did not come up in time")


def _shoot(base: str, browser_path: str | None, shots: dict[str, str], out: Path, width: int) -> None:
    from playwright.sync_api import sync_playwright
    from seed_demo import DEMO_EMAIL, DEMO_PASSWORD

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=browser_path) if browser_path else p.chromium.launch()
        try:
            phone = width < 600
            context = browser.new_context(
                viewport={"width": width, "height": 844 if phone else 900},
                device_scale_factor=2,
                is_mobile=phone,
                has_touch=phone,
            )
            page = context.new_page()
            page.add_init_script("try { localStorage.setItem('virgil-theme', 'dark') } catch (e) {}")
            page.goto(f"{base}/login")
            page.fill("input[name=username]", DEMO_EMAIL)
            page.fill("input[name=password]", DEMO_PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            for name, path in shots.items():
                page.goto(base + path)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1200)  # charts animate in
                if "/login" in page.url:
                    raise SystemExit(f"{name}: not signed in")
                page.screenshot(path=str(out / f"{name}.png"), full_page=True)
                print(f"shot {name}")
        finally:
            browser.close()


def _compress(names, out: Path) -> None:
    """A flat dark UI loses nothing visible in a 256-colour palette and shrinks about 5x."""
    from PIL import Image

    for name in names:
        path = out / f"{name}.png"
        image = Image.open(path).convert("RGB")
        image.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG).save(
            path, optimize=True
        )
        print(f"{name}.png {path.stat().st_size // 1024} KB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8199, help="port for the throwaway instance")
    parser.add_argument("--browser", help="path to an installed Chromium instead of Playwright's")
    parser.add_argument("--width", type=int, default=1280, help="viewport width; below 600 shoots as a phone")
    parser.add_argument("--out", type=Path, default=README_DIR, help="output directory (default: docs/screenshots)")
    parser.add_argument(
        "pages", nargs="*", help=f"pages to shoot: {', '.join([*PAGES, *EXTRA])} (default: the README set)"
    )
    args = parser.parse_args()
    known = {**PAGES, **EXTRA}
    unknown = set(args.pages) - set(known)
    if unknown:
        parser.error(f"unknown page(s): {', '.join(sorted(unknown))}")
    shots = {name: known[name] for name in (args.pages or PAGES)}
    args.out.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="virgil-screenshots-"))
    env = {
        **os.environ,
        "VIRGIL_ENV": "local",
        "VIRGIL_CENTRAL_DB_PATH": str(workdir / "virgil-central.db"),
        "VIRGIL_SECOND_BRAIN_PATH": "",
        "VIRGIL_API_KEY": "",
    }
    server = None

    def stop(*_):
        raise SystemExit(130)

    signal.signal(signal.SIGTERM, stop)
    try:
        subprocess.run([sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env, check=True)  # noqa: S603
        base = f"http://127.0.0.1:{args.port}"
        server = subprocess.Popen(  # noqa: S603
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_until_up(args.port, server)
        sys.path.insert(0, str(ROOT / "scripts"))
        _shoot(base, args.browser, shots, args.out, args.width)
        _compress(shots, args.out)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
