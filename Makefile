.PHONY: venv install fixtures test server clean

PY := .venv/bin/python

venv:
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"

install: venv

fixtures:
	$(PY) scripts/generate_fixtures.py

test:
	$(PY) -m pytest

server:
	CLOCKALIGN_HOME=$${CLOCKALIGN_HOME:-./data} $(PY) scripts/run_server.py

clean:
	rm -rf data build src/*.egg-info src/clockalign/__pycache__
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
