.PHONY: install test smoke run clean

install:
	python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

test:
	. .venv/bin/activate && python -m pytest

smoke:
	. .venv/bin/activate && python scripts/smoke.py

run:
	./scripts/run_local.sh

clean:
	rm -rf data .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
