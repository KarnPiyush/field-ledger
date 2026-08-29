FROM ghcr.io/astral-sh/uv:0.11.28 AS uv
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY apps/api ./apps/api
COPY demo/fixtures ./demo/fixtures
RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["fieldledger-api"]
