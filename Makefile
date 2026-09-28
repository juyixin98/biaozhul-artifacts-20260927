.PHONY: install test verify clean run replay

PYTHON ?= python3

install:
	$(PYTHON) -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

test:
	. .venv/bin/activate && python -m pytest -q

verify:
	. .venv/bin/activate && python scripts/verify.py

run:
	. .venv/bin/activate && uvicorn basefee_model.api.main:app --host 127.0.0.1 --port 8000

replay:
	. .venv/bin/activate && python -m basefee_model.cli replay data/canonical_fixture.json --db basefee_model.db --request-id make-run

clean:
	rm -rf .pytest_cache **/__pycache__ *.db
