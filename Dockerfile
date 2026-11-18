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

# INSTALL_BROKER=1 additionally installs the Kotak Neo SDK.
#
# It comes from the VENDORED wheel in vendor/, NOT from pyproject's [broker] extra: GitHub
# blocked the upstream repo (github.com/Kotak-Neo/Kotak-neo-api-v2) on 2026-11-05 --
# "Repository access blocked", reason private_information -- so the pinned git+https URL can no
# longer be cloned. An unauthenticated clone now dies with
#   fatal: could not read Username for 'https://github.com': No such device or address
# which fails the build. The wheel is the same neo-api-client 2.0.0 build the last working
# image shipped, repacked from its installed site-packages tree. See vendor/README.md.
#
# Default 0 so the image builds without the SDK (paper mode works without it).
ARG INSTALL_BROKER=0
COPY vendor ./vendor
RUN pip install --upgrade pip && \
    if [ "$INSTALL_BROKER" = "1" ]; then \
        pip install ".[postgres]" ./vendor/neo_api_client-2.0.0-py3-none-any.whl; \
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
