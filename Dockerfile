FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY app/ ./app/

RUN adduser --disabled-password --no-create-home --gecos "" appuser
RUN mkdir -p /data/users && chown -R appuser:appuser /data
USER appuser

ARG GIT_SHA=unknown
ENV VIRGIL_GIT_SHA=$GIT_SHA
ENV VIRGIL_ENV=prod
ENV UV_CACHE_DIR=/tmp/uv-cache

EXPOSE 8123

# Run the venv's python directly — `uv run` re-validates uv.lock at startup and
# tries to REWRITE it when the lock was generated under a different global uv
# config (e.g. a developer machine with exclude-newer set). /app is root-owned
# and the process runs as appuser, so that write crash-loops the container.
CMD ["/app/.venv/bin/python", "-m", "app"]
