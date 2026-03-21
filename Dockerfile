FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable

COPY app/ ./app/

RUN adduser --disabled-password --no-create-home --gecos "" appuser
RUN mkdir -p /data && chown appuser:appuser /data
USER appuser

ENV VIRGIL_ENV=prod
ENV UV_CACHE_DIR=/tmp/uv-cache

EXPOSE 8123

CMD ["uv", "run", "python", "-m", "app"]
