# syntax=docker/dockerfile:1

FROM python:3.11-slim AS base

# System deps: build tools for any wheels + tini for clean signal handling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY src ./src

# INSTALL_BROKER=1 additionally installs the Kotak Neo SDK from its pinned GitHub tag.
# Default 0 so the image builds without the external SDK (paper mode works without it).
ARG INSTALL_BROKER=0
RUN pip install --upgrade pip && \
    if [ "$INSTALL_BROKER" = "1" ]; then \
        pip install ".[postgres,broker]"; \
    else \
        pip install ".[postgres]"; \
    fi

# Runtime data/log/scrip directories (also mountable as volumes).
RUN mkdir -p /app/data /app/logs /app/scrip_cache

# Non-root user for safety.
RUN useradd --create-home --uid 1000 algo && chown -R algo:algo /app
USER algo

EXPOSE 8501

ENTRYPOINT ["tini", "--"]
# Default command runs the trading loop; the dashboard service overrides this in compose.
CMD ["python", "-m", "algo_trading.entrypoints.run_algo"]
