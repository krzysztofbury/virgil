import asyncio
import contextlib
import re
from contextlib import asynccontextmanager
from pathlib import Path

import markupsafe
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import AuthMiddleware
from app.csrf import CSRFMiddleware
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.central_db import close_central_db, init_central_db, promote_admin_emails

    await init_central_db()
    await promote_admin_emails()
    from app.services.scheduler import scheduler_loop

    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await close_central_db()


app = FastAPI(title="Virgil", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(CSRFMiddleware)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["md"] = _md_inline
templates.env.filters["strip_md"] = lambda t: re.sub(r"\*\*(.+?)\*\*", r"\1", t)
templates.env.filters["md_block"] = _md_block


@app.middleware("http")
async def inject_feature_flags(request, call_next):
    user_db = getattr(request.state, "user_db", None)
    if user_db:
        from app.db import get_feature_flags

        request.state.features = await get_feature_flags(user_db)
    else:
        request.state.features = {}
    return await call_next(request)


from fastapi.responses import FileResponse  # noqa: E402


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


from app.routers import (  # noqa: E402
    admin,
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
