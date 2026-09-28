.PHONY: install test verify replay fixtures api clean

install:
	python -m pip install -r requirements.txt
	python -m pip install -e .

fixtures:
	PYTHONPATH=src:. python scripts/build_fixtures.py

test:
	PYTHONPATH=src:. python -m pytest -q

verify:
	PYTHONPATH=src:. python scripts/verify.py

replay:
	PYTHONPATH=src:. python -m scripts.replay_blocks \
	  --scenario fixtures/chain_fixture.json --report reports/replay_cli.json

api:
	PYTHONPATH=src BASEFEE_DB=data/chain.db \
	  python -m uvicorn basefee.api.app:app --host 127.0.0.1 --port 8000

clean:
	rm -rf reports/*.json data/*.db src/*.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
