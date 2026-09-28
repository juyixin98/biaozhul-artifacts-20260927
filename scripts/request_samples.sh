#!/usr/bin/env bash
# Reproducible request samples against a locally running service.
# Usage:
#   1. (venv) uvicorn textindex.api:app --app-dir src --reload
#   2. bash scripts/request_samples.sh
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== version (pinned Unicode data) =="
curl -s "$BASE/version" | python3 -m json.tool

echo; echo "== create document with a flag + combining sequence (JSON) =="
curl -s -X POST "$BASE/documents" \
  -H 'content-type: application/json' \
  --data '{"text":"a🇺🇸é","doc_id":"demo","normalization":"NONE"}' \
  | python3 -m json.tool

echo; echo "== list grapheme clusters with byte/cp spans =="
curl -s "$BASE/documents/demo/clusters" | python3 -m json.tool

echo; echo "== convert: cluster 1 -> byte offset (start of the flag) =="
curl -s -X POST "$BASE/documents/demo/convert" \
  -H 'content-type: application/json' \
  --data '{"position":1,"from_unit":"grapheme","to_unit":"byte"}' \
  | python3 -m json.tool

echo; echo "== convert: byte 3 (inside the flag's UTF-8 sequence) -> 422 =="
curl -s -X POST "$BASE/documents/demo/convert" \
  -H 'content-type: application/json' \
  --data '{"position":3,"from_unit":"byte","to_unit":"grapheme"}' \
  | python3 -m json.tool

echo; echo "== convert: byte 10 (combining-mark lead, not a cluster edge) =="
curl -s -X POST "$BASE/documents/demo/convert" \
  -H 'content-type: application/json' \
  --data '{"position":10,"from_unit":"byte","to_unit":"grapheme","strict":true}' \
  | python3 -m json.tool

echo; echo "== raw invalid UTF-8 -> 400 input_error/invalid_utf8 =="
printf '\xed\xa0\x80' | curl -s -X POST "$BASE/documents/raw" \
  -H 'content-type: application/octet-stream' --data-binary @- \
  | python3 -m json.tool

echo; echo "== edit at grapheme boundary: insert combining acute after 'a' =="
curl -s -X POST "$BASE/documents/demo/edit" \
  -H 'content-type: application/json' \
  --data '{"start":1,"end":1,"replacement":"́","unit":"grapheme"}' \
  | python3 -m json.tool

echo; echo "== validate stored index against a fresh full rebuild =="
curl -s -X POST "$BASE/documents/demo/validate" | python3 -m json.tool

echo; echo "== version history (digest-bound revisions) =="
curl -s "$BASE/documents/demo/versions" | python3 -m json.tool
