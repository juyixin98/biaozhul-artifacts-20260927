.PHONY: install test run demo clean

PY := .venv/bin/python

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt

test:
	$(PY) -m pytest tests/ -v

run:
	PYTHONPATH=app $(PY) -m archguard --config config.example.json

demo:
	bash scripts/demo.sh

clean:
	rm -rf var var-cli tests/logs .pytest_cache
