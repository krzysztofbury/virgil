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

import itertools
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from screenshots import _wait_until_up  # noqa: E402
from seed_demo import DEMO_EMAIL, DEMO_PASSWORD  # noqa: E402

PAGES = {
    "dashboard": "/",
    "daily": "/daily",
    "training": "/training",
    "freedom": "/freedom",
    "finance": "/finance",
    "finance setup": "/finance/setup",
}
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
        "VIRGIL_LANDING_PAGE": "true",
        "VIRGIL_CONTACT_EMAIL": "contact@example.com",
        # Mail is logged, so a test can open the link it carries.
        "VIRGIL_MAIL_BACKEND": "log",
        # Each browser context arrives as its own client (_new_context): the suite as
        # a whole makes more requests in a minute than one reader's rate limit allows.
        "VIRGIL_TRUST_CLOUDFLARE_HEADERS": "true",
        "VIRGIL_TRUSTED_PROXY_IPS": "127.0.0.1",
    }
    subprocess.run(  # noqa: S603
        [sys.executable, "scripts/seed_demo.py"], cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL
    )
    log_path = work / "server.log"
    log = log_path.open("wb")
    server = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_up(port, server)
        with playwright_api.sync_playwright() as playwright:
            browser = _launch(playwright)
            try:
                base = f"http://127.0.0.1:{port}"
                yield browser, base, _sign_in(browser, base), log_path
            finally:
                browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


_clients = itertools.count(1)


def _new_context(browser, **options):
    client = {"CF-Connecting-IP": f"198.51.100.{next(_clients) % 250 + 1}"}
    return browser.new_context(extra_http_headers=client, **options)


def _signed_in(site, width):
    browser, base, session, _log = site
    phone = width < 600
    context = _new_context(
        browser, viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone, storage_state=session
    )
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda message: message.type == "error" and problems.append(message.text))
    page.on("pageerror", lambda error: problems.append(str(error)))
    return context, page, problems, base


def _sign_in(browser, base):
    """One sign-in for the module, saved and reused by every context."""
    context = _new_context(browser)
    try:
        page = context.new_page()
        page.goto(f"{base}/login")
        # Email first, then the password (#168).
        page.fill("input[name=username]", DEMO_EMAIL)
        page.click("button[type=submit]")
        page.fill("input[name=password]", DEMO_PASSWORD)
        page.click("form[action='/login'] button[type=submit]")
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


def test_the_life_radar_draws_filled_shapes(site):
    """Points alone can pass for a chart: the shapes are what a broken curve setting loses."""
    context, page, _problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        page.goto(f"{base}/")
        page.wait_for_load_state("networkidle")
        painted = page.evaluate(
            """() => {
                const canvas = document.getElementById('radarChart');
                const chart = Chart.getChart(canvas);
                chart.stop();
                chart.update('none');
                const { data } = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height);
                let count = 0;
                for (let i = 3; i < data.length; i += 4) if (data[i] > 40) count += 1;
                return count / (canvas.width * canvas.height);
            }"""
        )
        assert painted > 0.1, f"only {painted:.1%} of the radar is painted"
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
def test_the_tray_stays_clear_of_the_page_and_success_clears_itself(site, width):
    context, page, problems, base = _signed_in(site, width)
    try:
        # Not the load event: the toast's 4 s run from DOMContentLoaded, and a
        # slow stylesheet or script can outlast them.
        page.goto(f"{base}/settings?tab=data&msg=Saved+by+the+smoke+test", wait_until="domcontentloaded")
        tray = page.locator("#notification-tray")
        toast = tray.locator(".feedback-message", has_text="Saved by the smoke test")
        toast.wait_for()
        assert tray.evaluate("el => getComputedStyle(el).position") == "fixed"

        # Measured after the slide-in, which moves it by a few pixels.
        toast.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
        box = toast.bounding_box()
        viewport = page.viewport_size
        assert box["y"] > viewport["height"] / 2, "it sits low, away from the heading and the tabs"
        assert box["height"] <= 64, "one line, not a banner"
        tabs = page.locator("main .tabs").first.bounding_box()
        assert box["y"] > tabs["y"] + tabs["height"], "the heading and tabs stay readable"
        bar = page.locator(".bottom-bar")
        if bar.is_visible():
            assert box["y"] + box["height"] <= bar.bounding_box()["y"], "and the bottom bar stays tappable"

        # Scrolling must not carry it away, and the page under an empty tray stays clickable.
        page.mouse.wheel(0, 600)
        assert abs(toast.bounding_box()["y"] - box["y"]) < 2
        toast.wait_for(state="detached", timeout=8000)
        assert tray.evaluate("el => getComputedStyle(el).pointerEvents") == "none", "the gaps must not swallow clicks"
        assert problems == []
    finally:
        context.close()


def test_controls_answer_on_press_and_a_confirmation_never_takes_a_click(site):
    context, page, _problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        page.goto(f"{base}/settings?tab=general&msg=Saved", wait_until="domcontentloaded")
        toast = page.locator(".feedback-success")
        toast.wait_for()
        assert toast.evaluate("el => getComputedStyle(el).pointerEvents") == "none"

        button = page.locator("main .btn").first
        button.hover()
        page.mouse.down()
        page.wait_for_timeout(200)
        transform = button.evaluate("el => getComputedStyle(el).transform")
        page.mouse.up()
        assert transform.startswith("matrix(0.97"), transform

        color = "document.querySelector('meta[name=theme-color]').content"
        assert page.evaluate(color) == "#06080d"
        page.click("#theme-toggle")
        assert page.evaluate(color) == "#ffffff", "the status bar follows the chosen theme"
        page.click("#theme-toggle")
    finally:
        context.close()


def test_the_daily_count_follows_only_the_routines_it_lists(site):
    context, page, _problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        page.goto(f"{base}/daily")
        counter = page.locator("#done-count")
        assert counter.inner_text().endswith("/3")
        routine = page.locator("[data-routines] .toggle-group").first
        state = routine.locator(".toggle-state")
        # Whatever a run left behind: bring it to Pending, so the next tap is Done.
        for _ in range(2):
            if state.inner_text() != "Pending":
                with page.expect_response(lambda r: r.url.endswith("/daily/save")):
                    routine.locator(".toggle-btn").click()
        before = int(counter.inner_text().split("/")[0])
        with page.expect_response(lambda r: r.url.endswith("/daily/save")):
            routine.locator(".toggle-btn").click()
        after = int(counter.inner_text().split("/")[0])
        assert after == before + 1
        with page.expect_response(lambda r: r.url.endswith("/daily/save")):
            page.locator(".andy-item--task .toggle-btn").first.click()
        assert int(counter.inner_text().split("/")[0]) == after, "an A.N.D.Y. task is not a routine"
    finally:
        context.close()


def test_a_narrow_phone_keeps_names_and_labels_whole(site):
    context, page, _problems, base = _signed_in(site, 320)
    try:
        page.goto(f"{base}/training")
        names = page.locator(".pb-movement-head h4")
        assert names.count() > 0
        for index in range(min(names.count(), 3)):
            box = names.nth(index).bounding_box()
            assert box["width"] > 60, "a name squeezed beside its load stacks letter by letter"
        page.goto(f"{base}/settings?tab=general")
        split = page.evaluate(
            """() => [...document.querySelectorAll('.settings-tabs a')]
                .filter(a => a.scrollWidth > a.clientWidth || getComputedStyle(a).overflowWrap !== 'normal')
                .map(a => a.textContent.trim())"""
        )
        assert split == [], "a tab label must fit whole, not break mid-word"
    finally:
        context.close()


def test_a_phone_gets_native_touch_behaviour(site):
    browser, base, session, _log = site
    context = _new_context(
        browser, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, storage_state=session
    )
    page = context.new_page()
    try:
        page.goto(f"{base}/goals")
        highlight = page.evaluate("getComputedStyle(document.documentElement).webkitTapHighlightColor")
        assert highlight in ("rgba(0, 0, 0, 0)", "transparent")
        button = page.locator(".bottom-bar .tab-item").first
        assert button.evaluate("el => getComputedStyle(el).touchAction") == "manipulation"
        assert button.evaluate("el => getComputedStyle(el).userSelect") == "none"
        # iOS zooms the page into any field under 16px and does not zoom back out.
        small = """() => [...document.querySelectorAll('input:not([type=hidden]), select, textarea')]
            .filter(el => parseFloat(getComputedStyle(el).fontSize) < 16)
            .map(el => el.name || el.className)"""
        assert page.evaluate(small) == []
        # Past the phone breakpoint on touch: a tablet, or a phone held sideways.
        page.set_viewport_size({"width": 820, "height": 600})
        page.goto(f"{base}/daily")
        assert page.evaluate(small) == []
    finally:
        context.close()


HOVER_GATE = "(hover: hover) and (pointer: fine)"
_BACKGROUND = "el => getComputedStyle(el).backgroundColor"


def test_a_tap_keeps_no_hover_style_and_a_mouse_still_gets_one(site):
    """A touch browser applies :hover on tap and keeps it, so hover styles wait
    for a hovering pointer (#201). The goal delete button, which only hover
    reveals, stays shown on a touch screen wide enough to have it."""
    browser, base, session, _log = site
    mouse = _new_context(browser, viewport={"width": 1280, "height": 900}, storage_state=session)
    touch = _new_context(
        browser, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, storage_state=session
    )
    try:
        page = mouse.new_page()
        page.goto(f"{base}/goals")
        assert page.evaluate(f"matchMedia('{HOVER_GATE}').matches")
        link = page.locator(".nav-links a:not(.active)").first
        page.mouse.move(0, page.viewport_size["height"] - 1)
        resting = link.evaluate(_BACKGROUND)
        link.hover()
        link.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
        assert link.evaluate(_BACKGROUND) != resting, "a mouse no longer gets the hover style"
        delete = page.locator(".goal-item .goal-delete-btn").first
        assert delete.evaluate("el => getComputedStyle(el).opacity") == "0"
        delete.locator("xpath=ancestor::article[contains(@class, 'goal-item')]").hover()
        delete.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
        assert delete.evaluate("el => getComputedStyle(el).opacity") == "1"

        page = touch.new_page()
        page.goto(f"{base}/goals")
        assert not page.evaluate(f"matchMedia('{HOVER_GATE}').matches")
        summary = page.locator("details summary").first
        resting = summary.evaluate(_BACKGROUND)
        summary.tap()
        summary.evaluate("el => Promise.all(el.getAnimations().map(a => a.finished))")
        assert summary.evaluate(_BACKGROUND) == resting, "the tapped control kept its hover style"
        # Past the phone breakpoint on touch, a tablet: nothing can hover the row.
        page.set_viewport_size({"width": 820, "height": 600})
        delete = page.locator(".goal-item .goal-delete-btn").first
        assert delete.is_visible()
        assert delete.evaluate("el => getComputedStyle(el).opacity") == "1"
    finally:
        mouse.close()
        touch.close()


def test_a_job_card_does_not_replay_its_entrance_on_every_poll(site):
    context, page, _problems, base = _signed_in(site, WIDTHS["desktop"])
    card = (
        '<article id="job-status-9"><div id="job-status-body-9" class="job-status-card job-status-queued"></div>'
        "</article>"
        '<div class="tray-poller" hx-get="/api/jobs/active" hx-trigger="every 500ms" '
        'hx-target="#job-notifications" hx-swap="innerHTML" hx-sync="#job-notifications:replace"></div>'
    )
    try:
        page.route("**/api/jobs/active*", lambda route: route.fulfill(content_type="text/html", body=card))
        page.add_init_script(
            """window.__entrances = 0;
            document.addEventListener('animationstart', event => {
                if (event.animationName === 'notification-enter') window.__entrances += 1;
            }, true);"""
        )
        page.goto(f"{base}/settings?tab=general")
        page.locator("#job-status-9").wait_for()
        page.wait_for_timeout(2500)
        assert page.evaluate("window.__entrances") == 1, "one card, one entrance, however many polls"
    finally:
        context.close()


def test_the_phone_menu_opens_over_a_waiting_notification(site):
    context, page, _problems, base = _signed_in(site, WIDTHS["phone"])
    try:
        page.goto(f"{base}/settings?tab=data&err=Stays+until+dismissed")
        page.locator(".feedback-error").wait_for()
        page.locator(".tab-item-wrap > button").first.click()
        menu = page.locator(".tab-dropdown.show")
        menu.wait_for()
        last = menu.locator("a, button").last
        last.evaluate("el => Promise.all(document.getAnimations().map(a => a.finished))")
        box = last.bounding_box()
        assert page.evaluate(
            "([x, y]) => document.elementFromPoint(x, y).closest('.tab-dropdown') !== null",
            [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2],
        ), "the tray sits over the menu"
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


def test_phone_tables_fit_and_the_header_keeps_log_out_away_from_settings(site):
    """From the design review: on a phone the exercise library and Oura history
    hid most of their columns, and Log out sat one icon from Settings."""
    context, page, problems, base = _signed_in(site, WIDTHS["phone"])
    try:
        for path in ("/settings?tab=configuration", "/oura"):
            page.goto(base + path)
            page.wait_for_load_state("networkidle")
            sideways = page.evaluate(
                """() => {
                    document.querySelectorAll('details').forEach(d => { d.open = true; });
                    document.querySelectorAll('[data-view-for]').forEach(v => { v.hidden = false; });
                    return [...document.querySelectorAll('.overflow-x')]
                        .filter(box => box.querySelector('table') && box.scrollWidth > box.clientWidth + 1)
                        .map(box => box.querySelector('table').className || 'table');
                }"""
            )
            assert sideways == [], f"{path}: tables still scroll sideways on a phone: {sideways}"

        header = page.locator("nav.top-nav")
        assert header.get_by_role("link", name="Settings").is_hidden()
        assert header.get_by_role("button", name="Log out").is_hidden()
        page.locator("button[aria-controls=tab-more-menu]").click()
        page.locator("#tab-more-menu").get_by_role("button", name="Log out").wait_for(state="visible")
        assert problems == []
    finally:
        context.close()


def test_a_json_export_restores_through_the_check_page(site, tmp_path):
    """#48: the upload is multipart, the confirm asks first, and the report fits a phone."""
    context, page, problems, base = _signed_in(site, WIDTHS["phone"])
    try:
        page.goto(f"{base}/settings?tab=data")
        with page.expect_download() as download:
            page.get_by_role("link", name="Download JSON").click()
        export = tmp_path / "export.json"
        download.value.save_as(export)

        page.set_input_files("input[name=file]", str(export))
        page.get_by_role("button", name="Check file").click()
        page.wait_for_url("**/settings/import/*")
        assert page.get_by_role("cell", name="Daily logs").count() == 1
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"the check page is {overflow}px wider than a phone"

        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_role("button", name="Replace my data").click()
        page.wait_for_url("**/settings?tab=data*")
        assert "Data restored" in page.content()
        assert problems == []
    finally:
        context.close()


def test_the_shortcut_overlay_shows_translations_as_text(site):
    """A translation is data: markup in one must not become elements."""
    context, page, problems, base = _signed_in(site, WIDTHS["desktop"])
    try:
        page.goto(f"{base}/daily")
        page.evaluate("""() => { i18nCatalog = {Dashboard: '<b id="injected">x</b>'}; }""")
        page.keyboard.press("?")
        overlay = page.locator(".shortcut-overlay")
        overlay.wait_for()
        assert '<b id="injected">x</b>' in overlay.inner_text()
        assert page.locator("#injected").count() == 0
        assert overlay.locator("kbd").count() >= 20 and "←" in overlay.inner_text()
        assert problems == []
    finally:
        context.close()


def _swipe(page, from_x, to_x):
    page.evaluate(
        """([fromX, toX]) => {
            const target = document.querySelector('#daily-form');
            const at = (x) => [new Touch({identifier: 1, target, clientX: x, clientY: 200})];
            target.dispatchEvent(new TouchEvent('touchstart', {bubbles: true, touches: at(fromX), changedTouches: at(fromX)}));
            target.dispatchEvent(new TouchEvent('touchend', {bubbles: true, touches: [], changedTouches: at(toX)}));
        }""",
        [from_x, to_x],
    )


def test_a_swipe_on_daily_follows_the_pages_own_day_links(site):
    browser, base, session, _log = site
    context = _new_context(browser, viewport={"width": 390, "height": 844}, has_touch=True, storage_state=session)
    try:
        page = context.new_page()
        page.goto(f"{base}/daily/2026-01-10")
        with page.expect_navigation():
            _swipe(page, 300, 100)
        assert page.url.endswith("/daily/2026-01-11")
        with page.expect_navigation():
            _swipe(page, 100, 300)
        assert page.url.endswith("/daily/2026-01-10")
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS.values(), ids=WIDTHS.keys())
def test_the_landing_page_renders_and_opens_the_request_panel(site, width):
    """A signed-out visitor at / sees the landing page (#168): no console errors,
    nothing wider than the screen, and Request access opens its panel."""
    browser, base, _session, _log = site
    phone = width < 600
    context = _new_context(browser, viewport={"width": width, "height": 844 if phone else 900}, is_mobile=phone)
    page = context.new_page()
    problems: list[str] = []
    page.on("console", lambda message: message.type == "error" and problems.append(message.text))
    page.on("pageerror", lambda error: problems.append(str(error)))
    try:
        page.goto(base + "/")
        page.wait_for_load_state("networkidle")
        assert page.url == base + "/"
        assert page.locator(".lp-hero h1").is_visible()
        assert page.evaluate("document.documentElement.scrollWidth - window.innerWidth") <= 0
        page.locator(".lp-hero .lp-request > summary").click()
        panel = page.locator(".lp-hero .lp-request-panel")
        assert panel.is_visible() and "contact@example.com" in panel.inner_text()
        # Above the fold the images are drawn; the lazy ones below must at least be served.
        assert page.evaluate(
            "[...document.querySelectorAll('img:not([loading=lazy])')].every(img => img.complete && img.naturalWidth > 0)"
        )
        for src in page.evaluate("[...document.querySelectorAll('img[loading=lazy]')].map(img => img.src)"):
            assert page.request.get(src).ok, src
        assert problems == []
    finally:
        context.close()


def test_a_sign_in_link_opened_in_the_same_browser_signs_in_without_a_click(site):
    """Asked for here, the emailed link signs in by itself (#168): the confirm
    page sends its own form, and the browser lands past /login."""
    browser, base, _session, log_path = site
    context = _new_context(browser)
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(str(error)))
    try:
        page.goto(f"{base}/login")
        page.fill("input[name=username]", DEMO_EMAIL)
        page.click("button[type=submit]")
        page.click("form[action='/login/link'] button[type=submit]")
        page.wait_for_selector(".auth-info")
        link = None
        for _ in range(50):
            found = re.findall(r"https?://\S+/login/link\?token=[\w-]+", log_path.read_text(errors="replace"))
            if found:
                link = found[-1]
                break
            time.sleep(0.1)
        assert link, "the sign-in link was logged"
        page.goto(link.replace(link.split("/login/")[0], base))
        page.wait_for_url(lambda url: "/login" not in url)
        assert problems == []
    finally:
        context.close()


@pytest.mark.parametrize("path", ["/login", "/signup"])
def test_signed_out_pages_run_their_scripts(site, path):
    """These pages stand alone, outside base.html: app.js there once stopped at
    its first translated string, so nothing after it ran."""
    browser, base, _session, _log = site
    context = _new_context(browser)
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(str(error)))
    try:
        page.goto(base + path)
        page.wait_for_load_state("networkidle")
        assert problems == []
    finally:
        context.close()
