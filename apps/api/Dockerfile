# FastAPI service — build from the REPO ROOT context so it can install the algo_trading engine.
# Usage: docker build -f apps/api/Dockerfile -t algo-api .
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends build-essential tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Install the engine (algo_trading) + Postgres driver, then the API deps.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install ".[postgres]" \
    && pip install "fastapi>=0.110" "uvicorn[standard]>=0.29" "pyjwt>=2.8" "python-multipart>=0.0.9"

COPY apps/api ./apps/api
ENV PYTHONPATH=/app/apps/api

RUN useradd --create-home --uid 1000 api && chown -R api:api /app
USER api

EXPOSE 8000
ENTRYPOINT ["tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
