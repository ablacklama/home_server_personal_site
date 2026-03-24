FROM python:3.10-slim AS base

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source and install the project itself
COPY README.md ./
COPY src/ src/
COPY scripts/ scripts/
RUN uv sync --frozen --no-dev

EXPOSE 8743

CMD ["uv", "run", "personal-site", "--host", "0.0.0.0", "--port", "8743"]
