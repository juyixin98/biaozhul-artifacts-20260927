#!/usr/bin/env bash
# Example requests against a locally running flowrouter.
# Start it first:
#   make run
# or:
#   go run ./cmd/flowrouter -config configs/config.example.yaml -flowsets testdata/flowsets -reset-db
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8080}"
j() { python3 -m json.tool; }

echo "## health"
curl -s "$BASE/healthz" | j

echo; echo "## resolve one five-tuple"
curl -s -X POST "$BASE/v1/route" -H 'Content-Type: application/json' \
  -d '{"src_ip":"10.0.0.1","dst_ip":"10.1.0.9","proto":6,"src_port":40001,"dst_port":443}' | j

echo; echo "## current version, members and BUCKET shares"
curl -s "$BASE/v1/version" | j

echo; echo "## structural bucket share vs realized TRAFFIC share"
curl -s -X POST "$BASE/v1/route/bulk" -H 'Content-Type: application/json' \
  --data @<(python3 -c 'import json;print(json.dumps({"flows":json.load(open("testdata/flowsets/flows_smoke.json"))["flows"]}))') >/dev/null
curl -s "$BASE/v1/stats" | j

echo; echo "## immediately exclude hop-a (optimistic concurrency: expected_version)"
curl -s -X POST "$BASE/admin/members/hop-a/down" -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"reason":"manual-maintenance"}' | j

echo; echo "## replay the fixed flow set: version 1 -> 2 (minimal migration vs modulo baseline)"
curl -s -X POST "$BASE/v1/replay" -H 'Content-Type: application/json' \
  -d '{"flow_set":"flows_smoke","from_version":1,"to_version":2}' | j

echo; echo "## versioned recovery (reassignment)"
curl -s -X POST "$BASE/admin/members/hop-a/up" -H 'Content-Type: application/json' \
  -d '{"expected_version":2}' | j

echo; echo "## list replay runs"
curl -s "$BASE/v1/runs" | j
