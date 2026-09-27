#!/usr/bin/env bash
# Minimal CLI usage examples. No server is required: both the online kernel
# and the independent offline oracle run directly against the JSON fixtures.
# Exit code: 0 = satisfied/pending, 1 = violated, 2 = usage/input error.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN=${BIN:-cargo run --release --}
RULES=fixtures/rulesets/shop-v1.json

trace_file() { # fixture name -> temp file {"steps":[...]}
  python3 -c "
import json,sys
d=json.load(open('fixtures/traces/$1'))
print(json.dumps({'steps': d['steps']}))"
}

echo "== online kernel, satisfied trace (exit 0) =="
trace_file a_boundary_satisfied.json > /tmp/trace-a.json
$BIN online "$RULES" /tmp/trace-a.json || true

echo
echo "== offline oracle, overlapping triggers -> violated (exit 1) =="
trace_file b_overlap_triggers.json > /tmp/trace-b.json
set +e
$BIN offline "$RULES" /tmp/trace-b.json
echo "oracle exit code: $?"
set -e

echo
echo "== malformed input reported as an input error (exit 2) =="
echo '{"steps":[{"index":42}]}' > /tmp/trace-bad.json
set +e
$BIN offline "$RULES" /tmp/trace-bad.json
echo "bad-input exit code: $? (expected 2)"
set -e
