FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PLANTCARE_DATA=/app/data \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY plantcare ./plantcare

EXPOSE 8000
CMD ["uvicorn", "plantcare.main:app", "--host", "0.0.0.0", "--port", "8000"]
