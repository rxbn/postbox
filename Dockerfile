# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv
FROM python:3.13-slim-bookworm AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --no-install-project
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm AS runtime
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=4s --start-period=5s \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ['MCP_PORT']+'/healthz', timeout=2).close()"]
ENTRYPOINT ["postbox"]
