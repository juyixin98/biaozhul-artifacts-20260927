.PHONY: setup test run clean

PYTHON ?= python3

setup:
	$(PYTHON) -m venv .venv
	. .venv/bin/activate && python -m pip install --upgrade pip && pip install -r requirements.txt

test:
	. .venv/bin/activate && python -m pytest tests/ -v

run:
	. .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8000

clean:
	rm -rf data .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
