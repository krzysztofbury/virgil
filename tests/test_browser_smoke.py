"""The main pages in a real browser: what string checks on CSS and JS cannot see.

Seeds a throwaway instance with scripts/seed_demo.py, serves it on a free port
and drives Chromium through Playwright: each page loads without console errors
or horizontal overflow at desktop and phone width, a Daily change saves over
HTMX and a lost one is reported, the notification tray behaves, and the pages
with Alpine components or page scripts run under the Content-Security-Policy.

Needs a Chromium: `uv run --locked --extra dev playwright install chromium`, or
VIRGIL_TEST_BROWSER=/path/to/chromium. Without one the module is skipped
locally; CI installs one and sets VIRGIL_REQUIRE_BROWSER so it cannot skip.
"""

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from screenshots import _wait_until_up  # noqa: E402
from seed_demo import DEMO_EMAIL, DEMO_PASSWORD  # noqa: E402

PAGES = {"dashboard": "/", "daily": "/daily", "training": "/training", "freedom": "/freedom"}
# Every other page with an Alpine component or a page script: under the
# Content-Security-Policy a refused script or expression shows up only as a
# console error.
CSP_PAGES = {
    "oura": "/oura",
    "blood": "/bloodwork/all?marker=1",
    "goals": "/goals",
    "experiment": "/experiments/1",
    "new experiment": "/experiments/new",
    "settings": "/settings",
    "library": "/settings?tab=configuration",
}
WIDTHS = {"desktop": 1280, "phone": 390}


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _launch(playwright):
    tried = []
    for executable in (os.environ.get("VIRGIL_TEST_BROWSER"), None, shutil.which("chromium")):
        if executable is None and tried:
            continue
        try:
            return playwright.chromium.launch(executable_path=executable)
        except playwright_api.Error as exc:
            tried.append(f"{executable or 'Playwright Chromium'}: {str(exc).splitlines()[0]}")
    reason = "No Chromium for the browser smoke test (" + "; ".join(tried) + ")"
    if os.environ.get("VIRGIL_REQUIRE_BROWSER"):
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    work = tmp_path_factory.mktemp("browser-instance")
    port = _free_port()
    env = {
        **os.environ,
        "VIRGIL_ENV": "local",
        "VIRGIL_CENTRAL_DB_PATH": str(work / "virgil-central.db"),
        "VIRGIL_BACKUP_DIR": str(work / "backups"),
        "VIRGIL_API_KEY": "",
    }
    subprocess.run(  # noqa: S603
        [sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL
    )
    server = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_up(port, server)
        with playwright_api.sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                base = f"http://127.0.0.1:{port}"
                yield browser, base, _sign_in(browser, base)
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def _signed_in(site, width):
    browser, base, session = site
    phone = width < 600
    context = browser.new_context(
        viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, storage_state=session
    )
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda message: message.type == "error" and problems.append(message.text))
    page.on("pageerror", lambda error: problems.append(str(error)))
    return context, page, problems, base


def _sign_in(browser, base):
    """One sign-in for the module: each one counts against the login rate limit."""
    context = browser.new_context()
    try:
        page = context.new_page()
        page.goto(f"{base}/login")
        page.fill("input[name=username]", DEMO_EMAIL)
        page.fill("input[name=password]", DEMO_PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(lambda url: "/login" not in url)
        return context.storage_state()
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS.values(), ids=WIDTHS.keys())
def test_main_pages_load_cleanly(site, width):
    context, page, problems, base = _signed_in(site, width)
    try:
        for name, path in PAGES.items():
            problems.clear()
            page.goto(base + path)
            page.wait_for_load_state("networkidle")
            assert "/login" not in page.url, f"{name}: not signed in"
            assert page.locator("main").count() == 1, f"{name}: no page content"
            assert problems == [], f"{name}: console errors {problems}"
            overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
            assert overflow <= 0, f"{name}: {overflow}px wider than the screen at {width}px"
            # A card that hides its overflow clips a value instead of widening the page.
            clipped = page.evaluate(
                """() => [...document.querySelectorAll('.card, .stat-card')]
                    .filter(el => getComputedStyle(el).overflowX === 'hidden' && el.scrollWidth > el.clientWidth + 1)
                    .map(el => (el.querySelector('h3, .stat-label') || el).textContent.trim().slice(0, 40))"""
            )
            assert clipped == [], f"{name}: content clipped at {width}px in {clipped}"
    finally:
        context.close()


def test_a_daily_change_saves_over_htmx_and_a_lost_one_is_reported(site):
    context, page, problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        page.goto(f"{base}/daily")
        toggle = page.locator("#daily-form .toggle-btn").first
        state = page.locator(f"#{toggle.get_attribute('aria-describedby')}")
        cycle = {"Pending": "Done", "Done": "Skipped", "Skipped": "Pending"}
        expected = cycle[state.inner_text()]

        with page.expect_response(lambda r: r.url.endswith("/daily/save")) as saved:
            toggle.click()
        assert saved.value.ok
        page.reload()
        assert state.inner_text() == expected, "the change did not survive a reload"

        # A request that never reaches the server must not look saved.
        assert problems == []
        page.route("**/daily/save", lambda route: route.abort())
        toggle.click()
        error = page.locator("#feedback-error")
        error.get_by_text("Network error. Your changes were not confirmed.").wait_for()
        page.wait_for_timeout(4500)  # past the success toast's 4 s
        assert error.locator(".feedback-message").count() == 1, "an error must wait for the user"
        error.locator("[data-feedback-dismiss]").click()
        assert error.locator(".feedback-message").count() == 0
        # The browser and htmx each log the aborted request, and nothing else.
        assert {p.split(" ")[0] for p in problems} <= {"Failed", "htmx:sendError", "htmx:afterRequest"}, problems
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS.values(), ids=WIDTHS.keys())
def test_the_tray_sits_in_the_top_right_and_success_clears_itself(site, width):
    context, page, problems, base = _signed_in(site, width)
    try:
        # Not the load event: the toast's 4 s run from DOMContentLoaded, and a
        # slow stylesheet or script can outlast them.
        page.goto(f"{base}/daily?msg=Saved+by+the+smoke+test", wait_until="domcontentloaded")
        tray = page.locator("#notification-tray")
        toast = tray.locator(".feedback-message", has_text="Saved by the smoke test")
        toast.wait_for()
        # Measured after the slide-in, which moves it by a few pixels.
        toast.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
        box = toast.bounding_box()
        assert box["x"] + box["width"] <= width and width - (box["x"] + box["width"]) < 40, "not at the right edge"
        assert box["y"] < 200, "not at the top"
        assert tray.evaluate("el => getComputedStyle(el).position") == "fixed"

        # Scrolling must not carry it away, and the page under an empty tray stays clickable.
        page.mouse.wheel(0, 600)
        assert abs(toast.bounding_box()["y"] - box["y"]) < 2
        toast.wait_for(state="detached", timeout=8000)
        corner = page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y).closest('#notification-tray') === null",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        )
        assert corner, "an empty tray swallows clicks in the corner"
        assert problems == []
    finally:
        context.close()


def test_scripted_pages_run_under_the_content_security_policy(site):
    context, page, problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        for name, path in CSP_PAGES.items():
            problems.clear()
            page.goto(base + path)
            page.wait_for_load_state("networkidle")
            assert problems == [], f"{name}: console errors {problems}"

        # An Alpine component reacting to input: the CSP build refuses what the
        # standard build allowed (x-model on a loop item's field, for one).
        page.goto(f"{base}/experiments/new")
        source = page.locator("input[name=source_matches]")
        assert source.is_visible(), "a minutes metric offers the Oura source field"
        page.select_option("select[name=metric_kinds]", "count")
        source.wait_for(state="hidden")
        page.locator("input[name=metric_targets]").wait_for(state="visible")
        assert problems == []
    finally:
        context.close()
