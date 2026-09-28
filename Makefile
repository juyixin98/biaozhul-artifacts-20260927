.PHONY: install unit integration test seed run replay golden clean

install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

unit:
	.venv/bin/python -m pytest tests/unit -v

integration:
	.venv/bin/python -m pytest tests/integration -v

test:
	.venv/bin/python -m pytest -v

golden:
	.venv/bin/python scripts/gen_golden_vectors.py

keygen:
	.venv/bin/python scripts/gen_dev_key.py

seed:
	.venv/bin/python scripts/seed_sample_data.py

replay:
	.venv/bin/python -m app.offline --db data/smt.db --public-key configs/dev_signing_key.pem.pub

run:
	.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080

clean:
	rm -rf data/*.db data/*.db-wal data/*.db-shm .pytest_cache
	find . -path ./.venv -prune -o -name '__pycache__' -type d -print -exec rm -rf {} +
