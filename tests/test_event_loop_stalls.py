"""Work that used to stop every request while it ran (#122).

Found with scripts/load_check.py: a logged traceback and a password check each
held the event loop, so the whole server paused for every other visitor.
"""

import asyncio
import os
import subprocess
import sys
import time

from app.auth import hash_password, verify_password_async


def test_the_server_logs_plain_lines_not_rich_tracebacks(tmp_path):
    """The MCP SDK installs Rich's handler on the root logger unless one is set
    first. Rich rendered each logged traceback with syntax highlighting on the
    event loop: 0.9 s for one failed import on a fast machine.

    A fresh process, because pytest's own capture handler on the root logger
    would make either configuration a no-op here.
    """
    probe = (
        "import logging, app.main, app.mcp_endpoint; "
        "print(','.join(type(h).__name__ for h in logging.getLogger().handlers))"
    )
    env = {**os.environ, "VIRGIL_CENTRAL_DB_PATH": str(tmp_path / "central.db"), "VIRGIL_API_KEY": ""}
    result = subprocess.run(  # noqa: S603 - this interpreter, fixed arguments
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=120, check=True
    )
    assert result.stdout.strip().splitlines()[-1] == "StreamHandler"


def test_a_password_check_leaves_the_event_loop_free():
    """bcrypt takes about 0.2 s; other requests must be served meanwhile."""
    stored = hash_password("correct horse battery")

    async def run():
        stalls = []

        async def tick():
            while True:
                started = time.perf_counter()
                await asyncio.sleep(0.005)
                stalls.append(time.perf_counter() - started - 0.005)

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.02)
        started = time.perf_counter()
        assert await verify_password_async("correct horse battery", stored)
        took = time.perf_counter() - started
        await asyncio.sleep(0.02)  # a tick held up by a blocking check lands only now
        ticker.cancel()
        return took, max(stalls)

    took, worst = asyncio.run(run())
    assert took > 0.05, "the check must actually have done bcrypt's work"
    assert worst < took / 2, f"the loop stalled {worst * 1000:.0f} ms during a {took * 1000:.0f} ms check"
