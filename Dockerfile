FROM python:3.14.10-slim@sha256:9aaa474097d094040649f2162a627c0049b408f52138d980916788d80f04802f

COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable

COPY app/ ./app/
COPY mcp_server/virgil_mcp.py ./mcp_server/virgil_mcp.py
COPY scripts/recovery.py ./scripts/recovery.py

RUN adduser --disabled-password --no-create-home --gecos "" appuser
RUN mkdir -p /data/users /backups && chown -R appuser:appuser /data /backups
USER appuser

ARG GIT_SHA=unknown
ENV VIRGIL_GIT_SHA=$GIT_SHA
ENV VIRGIL_ENV=prod
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV LITELLM_LOCAL_MODEL_COST_MAP=true

EXPOSE 8123

# Run the venv's python directly — `uv run` re-validates uv.lock at startup and
# tries to REWRITE it when the lock was generated under a different global uv
# config (e.g. a developer machine with exclude-newer set). /app is root-owned
# and the process runs as appuser, so that write crash-loops the container.
CMD ["/app/.venv/bin/python", "-m", "app"]
