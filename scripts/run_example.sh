#!/usr/bin/env bash
# Reproduce a verification request against a running server.
# Usage: scripts/run_example.sh [base_url]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ="$HERE/../fixtures/request_example.json"

curl -sS -X POST "$BASE/api/v1/verify" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: example-run-0001" \
  --data-binary @"$REQ" | python3 -m json.tool
