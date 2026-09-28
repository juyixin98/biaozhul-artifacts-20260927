#!/usr/bin/env bash
# Local example requests. Run after:
#   python -m venv .venv && . .venv/bin/activate
#   pip install -r requirements.txt
#   python -m scripts.seed_db --activate
#   uvicorn app.api:app --reload
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}

echo "1) health"
curl -s "$BASE/health" | python3 -m json.tool

echo "2) classic adjacent swap with path"
curl -s -X POST "$BASE/correct" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-teh-001' \
  -d '{"query": "teh", "threshold": 1.0}' | python3 -m json.tool

echo "3) multi-token misspelling"
curl -s -X POST "$BASE/correct" \
  -H 'Content-Type: application/json' \
  -d '{"query": "teh recieve", "threshold": 1.5}' | python3 -m json.tool

echo "4) repeated-character insertion"
curl -s -X POST "$BASE/correct" \
  -H 'Content-Type: application/json' \
  -d '{"query": "bananna", "threshold": 1.5}' | python3 -m json.tool

echo "5) diagnostics (variant, bounds, cost model, storage location)"
curl -s "$BASE/diagnostics" | python3 -m json.tool

echo "6) failure category: unsupported character (section sign does not normalize)"
curl -s -X POST "$BASE/correct" \
  -H 'Content-Type: application/json' \
  -d '{"query": "cafe§"}' | python3 -m json.tool

echo "7) normalization: accented letters fold into the alphabet"
curl -s -X POST "$BASE/correct" \
  -H 'Content-Type: application/json' \
  -d '{"query": "Ｈello", "threshold": 1.0}' | python3 -m json.tool
