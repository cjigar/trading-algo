.PHONY: install install-broker lint type test check run clean \
        docker-build docker-up docker-down docker-logs docker-ps

install:
	python3 -m pip install -e ".[dev]"

install-broker:
	python3 -m pip install -e ".[dev,broker]"

lint:
	ruff check src tests

fmt:
	ruff check --fix src tests

type:
	mypy

test:
	pytest

check: lint type test

run:
	python3 -m algo_trading.entrypoints.run_algo

# Read-only live-API validation (no orders). Requires the broker SDK + credentials + index tokens.
validate-live:
	python -m algo_trading.tools.validate_live

# Import today's trades from the Kotak account into the DB (read-only; NO orders placed).
import-trades:
	python -m algo_trading.tools.import_trades

# Import today's order book from the Kotak account into the DB (read-only; NO orders placed).
import-orders:
	python -m algo_trading.tools.import_orders

# Capture the live NIFTY option chain (OI/LTP) into the DB — NO strategy, NO orders.
capture:
	python -m algo_trading.entrypoints.run_capture

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# --- Docker (Postgres + loop + dashboard) ---
docker-build:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f algo dashboard

docker-ps:
	docker compose ps
