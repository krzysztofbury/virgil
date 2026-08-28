import asyncio
import contextlib
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import markupsafe
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthMiddleware
from app.csrf import CSRFMiddleware
from app.formatting import format_duration_seconds
from app.rate_limit import RateLimitMiddleware
from app.security_headers import SecurityHeadersMiddleware

BASE_DIR = Path(__file__).parent


def _apply_inline_md(html: str) -> str:
    """Apply bold+italic, bold, italic markdown to an HTML string."""
    html = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", html)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", html)
    return html


def _md_inline(text: str) -> markupsafe.Markup:
    """Convert inline markdown (bold, italic) to HTML."""
    escaped = str(markupsafe.escape(text))
    return markupsafe.Markup(_apply_inline_md(escaped))


def _md_block(text: str) -> markupsafe.Markup:
    """Convert markdown block (headers, lists, bold, italic, paragraphs) to HTML."""
    escaped = str(markupsafe.escape(text))
    lines = escaped.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append("")
            continue
        # Headers
        if stripped.startswith("### "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h4>{stripped[4:]}</h4>")
        elif stripped.startswith("## "):
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<h3>{stripped[3:]}</h3>")
        elif re.match(r"^[-*] ", stripped):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            html_lines.append(f"<li>{stripped[2:]}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            html_lines.append(f"<p>{stripped}</p>")
    if in_list:
        html_lines.append("</ul>")
    result = "\n".join(html_lines)
    return markupsafe.Markup(_apply_inline_md(result))


_APP_VERSION: str | None = None


def get_app_version() -> str:
    """Build version: VIRGIL_GIT_SHA (baked at image build) → git → 'unknown'. Cached."""
    global _APP_VERSION
    if _APP_VERSION is None:
        version = os.environ.get("VIRGIL_GIT_SHA", "") or "unknown"
        if version == "unknown":
            with contextlib.suppress(Exception):
                import subprocess

                version = (
                    subprocess.check_output(
                        ["git", "rev-parse", "--short", "HEAD"],
                        cwd=BASE_DIR.parent,
                        stderr=subprocess.DEVNULL,
                        text=True,
                        timeout=5,
                    ).strip()
                    or "unknown"
                )
        if not os.environ.get("VIRGIL_GIT_SHA"):
            # Dev run: the SW caches /static/ cache-first under a name keyed on
            # this version, and a bare git sha only changes per commit — suffix
            # the start time so every restart serves fresh static assets.
            version = f"{version}-{int(time.time())}"
        _APP_VERSION = version
    return _APP_VERSION


async def _start_application(app: FastAPI):
    from app.central_db import init_central_db, promote_admin_emails

    # Version banner — proves WHICH code is running in the deploy logs.
    _log = logging.getLogger("uvicorn")
    _version = get_app_version()

    app.state.central_migration_failure = False
    app.state.migration_failures = []
    try:
        await init_central_db()
        await promote_admin_emails()
    except Exception:
        app.state.central_migration_failure = True
        logging.getLogger(__name__).exception("Central database migration failed")

    # Run pending migrations for EXISTING per-user databases. Without this,
    # migrations only ever ran at account creation — new migrations silently
    # never reached older databases. ALL users are migrated, not just active
    # ones: a disabled account that gets re-enabled must not wake up with a
    # stale schema.
    from app.central_db import get_all_users
    from app.migrations.runner import count_pending_migrations, run_migrations
    from app.services.backup import snapshot_before_migration
    from app.user_db import close_user_db, open_user_db

    _migrated = 0
    if not app.state.central_migration_failure:
        for _user in await get_all_users():
            # open_user_db lives INSIDE the try: one corrupt/unreadable database
            # must degrade that account (visible via /healthz), never abort the
            # whole lifespan and take every other user down with it.
            _udb = None
            try:
                _udb = await open_user_db(_user["db_filename"])
                if await count_pending_migrations(_udb) > 0:
                    # Image rollback cannot reverse a migration — keep a snapshot
                    # of the pre-migration database next to the regular backups.
                    await snapshot_before_migration(_udb)
                await run_migrations(_udb)
                _migrated += 1
            except Exception:
                logging.getLogger(__name__).exception("Startup migration failed for %s", _user["db_filename"])
                # Surfaced via /healthz — a green healthcheck must not hide a user
                # whose every request will fail on missing tables.
                app.state.migration_failures.append(_user["db_filename"])
            finally:
                if _udb is not None:
                    await close_user_db(_udb)

    _log.info(
        "Virgil version=%s — central migration=%s, user migrations OK for %d DB(s), %d failed",
        _version,
        "failed" if app.state.central_migration_failure else "ok",
        _migrated,
        len(app.state.migration_failures),
    )

    from app.services.scheduler import scheduler_loop

    return None if app.state.central_migration_failure else asyncio.create_task(scheduler_loop())


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.central_db import close_central_db

    task = None
    try:
        task = await _start_application(app)
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await close_central_db()


class CentralMigrationGuardMiddleware:
    """Pure ASGI quarantine that preserves downstream cancellation semantics."""

    def __init__(self, application, state):
        self.application = application
        self.state = state

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and getattr(self.state, "central_migration_failure", False)
            and scope["path"] != "/healthz"
        ):
            response = JSONResponse({"status": "unavailable"}, status_code=503)
            await response(scope, receive, send)
            return
        await self.application(scope, receive, send)


app = FastAPI(title="Virgil", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.add_middleware(CentralMigrationGuardMiddleware, state=app.state)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Cache-buster for /static/ links: the SW serves them cache-first, so only a
# changed URL guarantees a fresh asset on the first load after an update.
templates.env.globals["app_version"] = get_app_version()
templates.env.filters["md"] = _md_inline
templates.env.filters["strip_md"] = lambda t: re.sub(r"\*\*(.+?)\*\*", r"\1", t)
templates.env.filters["md_block"] = _md_block
templates.env.filters["duration"] = format_duration_seconds


@app.get("/healthz")
async def healthz(request: Request):
    """Deployment health: 503 while any user DB failed its startup migrations,
    so a broken schema rollout stops the deploy instead of passing a /login ping."""
    degraded = bool(
        getattr(request.app.state, "central_migration_failure", False)
        or getattr(request.app.state, "migration_failures", [])
    )
    return JSONResponse({"status": "degraded" if degraded else "ok"}, status_code=503 if degraded else 200)


@app.get("/service-worker.js")
async def service_worker():
    """Serve the SW with the build version injected into CACHE_NAME —
    each deploy gets a fresh cache name, so stale static assets are purged
    automatically on activation (no more manual 'virgil-vN' bumps)."""
    sw_source = (BASE_DIR / "static" / "service-worker.js").read_text()
    return Response(
        sw_source.replace("{{SW_VERSION}}", get_app_version()),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


from app.routers import (  # noqa: E402
    admin,
    api,
    auth,
    bloodwork,
    daily,
    dashboard,
    experiments,
    feniks,
    goals,
    life_scores,
    onboarding,
    oura,
    oura_webhook,
    settings,
    training,
)

app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(daily.router)
app.include_router(training.router)
app.include_router(feniks.router)
app.include_router(oura.router)
app.include_router(oura_webhook.router)
app.include_router(bloodwork.router)
app.include_router(life_scores.router)
app.include_router(goals.router)
app.include_router(experiments.router)
app.include_router(settings.router)
app.include_router(onboarding.router)
app.include_router(api.router)
