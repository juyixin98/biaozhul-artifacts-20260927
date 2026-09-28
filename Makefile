.PHONY: help install test test-unit test-integration demo run clean

help:
	@echo "Targets:"
	@echo "  make install         create .venv and install dependencies"
	@echo "  make test            run unit + integration tests"
	@echo "  make test-unit       run unit tests only"
	@echo "  make test-integration run integration tests only"
	@echo "  make demo            run the end-to-end synthetic demo"
	@echo "  make run             start the FastAPI service (127.0.0.1:8000)"
	@echo "  make clean           remove caches and the local sqlite db"

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

test:
	.venv/bin/python -m pytest

test-unit:
	.venv/bin/python -m pytest tests/unit -m unit

test-integration:
	.venv/bin/python -m pytest tests/integration -m integration

demo:
	.venv/bin/python scripts/demo.py

run:
	.venv/bin/python run.py

clean:
	rm -rf .pytest_cache **/__pycache__ data/*.db
