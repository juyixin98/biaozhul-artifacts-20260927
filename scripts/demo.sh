#!/usr/bin/env bash
# End-to-end local demo: two managers, conflict, force takeover, reconciliation.
# Usage: scripts/demo.sh [base-url]
set -euo pipefail
BASE="${1:-http://localhost:8080}"
KIND=widget
NAME=demo-$RANDOM
j() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. health =="
curl -fsS "$BASE/healthz"; echo

echo "== 1. declare schema (map / set / atomic lists) =="
curl -fsS -XPUT "$BASE/v1/schemas/$KIND" \
  -H 'content-type: application/json' \
  -d '{"lists":{
        "ingresses":{"type":"map","key":"name"},
        "tags":{"type":"set"},
        "servers":{"type":"atomic"}}}'; echo

echo "== 2. manager 'net' applies baseline =="
curl -fsS -XPOST "$BASE/v1/$KIND/$NAME/apply" \
  -H 'content-type: application/json' \
  -d '{"manager":"net","config":{
        "image":"registry/widget:1","replicas":3,
        "ingresses":[{"name":"edge-1","host":"a.example","port":80}],
        "tags":["canary"],
        "servers":[{"zone":"z1","weight":10}]}}' | j; echo

echo "== 3. manager 'sre' writes net-owned image WITHOUT force -> 409 conflict =="
curl -sS -o /tmp/conflict.json -w 'HTTP %{http_code}\n' \
  -XPOST "$BASE/v1/$KIND/$NAME/apply" \
  -H 'content-type: application/json' \
  -d '{"manager":"sre","config":{"image":"registry/widget:2","replicas":5}}'
cat /tmp/conflict.json | j; echo

echo "== 4. same request WITH force -> takeover, unrelated fields preserved =="
curl -fsS -XPOST "$BASE/v1/$KIND/$NAME/apply" \
  -H 'content-type: application/json' \
  -d '{"manager":"sre","force":true,"config":{
        "image":"registry/widget:2","replicas":5}}' | j; echo

echo "== 5. ownership matrix =="
curl -fsS "$BASE/v1/$KIND/$NAME/ownership" | j; echo

echo "== 6. history (auditable revisions) =="
curl -fsS "$BASE/v1/$KIND/$NAME/history" | j; echo

echo "== resource name was: $KIND/$NAME =="
