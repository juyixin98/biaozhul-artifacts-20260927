.PHONY: venv install test validate serve demo clean

PY := .venv/bin/python

venv:
	python3 -m venv .venv

install: venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -r requirements.txt

test:
	$(PY) -m pytest -q

validate:
	rm -rf data/validation results/validation.json
	$(PY) scripts/validate.py

serve:
	$(PY) -m uvicorn zindex.api:app --host 127.0.0.1 --port 8000 --reload

demo:
	bash scripts/serve_demo.sh

clean:
	rm -rf data results/validation.json .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
