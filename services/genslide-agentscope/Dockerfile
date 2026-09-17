ARG BASE_IMAGE=python:3.12-slim
FROM ghcr.io/astral-sh/uv:0.10.2 AS uv
FROM ${BASE_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
RUN useradd --uid 10001 --user-group --no-create-home --home-dir /tmp python
ENV PATH="/app/.venv/bin:$PATH"
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "genslide_agentscope.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "64", "--backlog", "128", "--timeout-graceful-shutdown", "45", "--no-access-log"]
