FROM python:3.14.7-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

COPY --from=ghcr.io/astral-sh/uv:0.12.7@sha256:95f2aa1fe59274951cfe9b0cbc7972e879ff1004bc8945d130a32eb0dbd85945 /uv /usr/local/bin/uv

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
