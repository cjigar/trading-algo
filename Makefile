.PHONY: install install-broker lint type test check run dashboard clean

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

dashboard:
	streamlit run src/algo_trading/entrypoints/run_dashboard.py

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
