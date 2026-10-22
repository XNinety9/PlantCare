# Claude Code CLI, installed from npm: the package ships a native binary, so Node is only needed to install it
FROM node:22-slim AS claude-cli
ARG CLAUDE_CODE_VERSION=2.1.308
RUN npm install -g --loglevel=error @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY --from=claude-cli /usr/local/lib/node_modules/@anthropic-ai/claude-code /opt/claude-code
RUN ln -s /opt/claude-code/bin/claude.exe /usr/local/bin/claude

# The container runs read-only with a non-root user and no home directory: the CLI writes its state to /tmp
ENV PYTHONUNBUFFERED=1 \
    PLANTCARE_DATA=/app/data \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    DISABLE_AUTOUPDATER=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY plantcare ./plantcare

EXPOSE 8000
CMD ["uvicorn", "plantcare.main:app", "--host", "0.0.0.0", "--port", "8000"]
