#!/usr/bin/env bash
# End-to-end verification script.
#
# 1. builds in release mode (falling back to debug)
# 2. runs cargo test (unit + independent integration tests)
# 3. runs the analyzer CLI over every fixture program and re-verifies each
#    emitted report with the independent evidence verifier
# 4. if a server address is reachable (or INTERVAL_ANALYZER_E2E=1), exercises
#    the HTTP API end to end
#
# Checks that cannot be executed in this environment are reported explicitly
# as SKIPPED, never silently marked as passed.

set -u
cd "$(dirname "$0")/.."

bin=target/release/interval-analyzer
echo "== building =="
if ! cargo build --release 2>/dev/null; then
  echo "release build failed; falling back to debug"
  cargo build
  bin=target/debug/interval-analyzer
fi

echo
echo "== cargo test =="
cargo test --all-targets || { echo "TESTS FAILED"; exit 1; }

echo
echo "== fixtures: analyze + evidence verify =="
status=0
mkdir -p target/fixture-reports
for p in fixtures/programs/*.isl; do
  stem=$(basename "$p" .isl)
  report="target/fixture-reports/${stem}.json"
  if ! "$bin" analyze "$p" --config config/default.toml > "$report"; then
    echo "ANALYZE FAILED: $p"
    status=1
    continue
  fi
  if "$bin" verify "$p" "$report" >/dev/null; then
    echo "  verified: $stem"
  else
    echo "EVIDENCE VERIFY FAILED: $stem"
    "$bin" verify "$p" "$report"
    status=1
  fi
done
if [ "$status" -ne 0 ]; then exit 1; fi

echo
echo "== negative evidence checks (tampered reports must be REJECTED) =="
p=fixtures/programs/branch_narrow.isl
report=target/fixture-reports/branch_narrow.json
python3 - "$report" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
r["program_hash"] = "0000000000000000"
json.dump(r, open(sys.argv[1] + ".tampered", "w"))
PY
if "$bin" verify "$p" "$report.tampered" >/dev/null 2>&1; then
  echo "ERROR: tampered report was accepted"
  exit 1
else
  echo "  tampered report correctly rejected"
fi

echo
echo "== HTTP end-to-end =="
addr="${INTERVAL_ANALYZER_ADDR:-127.0.0.1:18080}"
if [ "${INTERVAL_ANALYZER_E2E:-0}" = "1" ] || curl -s -o /dev/null "http://$addr/health" 2>/dev/null; then
  "$bin" serve --config config/default.toml "$addr" &
  srv=$!
  trap 'kill $srv 2>/dev/null' EXIT
  # wait for readiness
  for _ in $(seq 1 50); do
    curl -s -o /dev/null "http://$addr/health" && break
    sleep 0.1
  done
  echo "-- /health"; curl -s "http://$addr/health"; echo
  echo "-- /v1/analyze (possible OOB)"
  resp=$(curl -s -X POST "http://$addr/v1/analyze" \
      -H 'content-type: application/json' \
      --data @fixtures/api/analyze_possible_oob.json)
  echo "$resp" | python3 -c '
import json, sys
r = json.load(sys.stdin)
assert r["request_id"].startswith("req-"), r
s = r["report"]["summary"]
assert s["possible_violations"] == 1 and s["definite_violations"] == 0, s
print("   request_id:", r["request_id"], "possible:", s["possible_violation_ids"])'
  echo "-- /v1/verify roundtrip"
  echo "$resp" | python3 -c '
import json, sys, urllib.request
r = json.load(sys.stdin)
# verify must be against the SAME source that produced the report
source = json.load(open("fixtures/api/analyze_possible_oob.json"))["source"]
body = json.dumps({"source": source, "report": r["report"]}).encode()
req = urllib.request.Request("http://'"$addr"'/v1/verify", data=body,
                             headers={"content-type": "application/json"})
v = json.load(urllib.request.urlopen(req))
assert v["verification"]["ok"], v
print("   verified:", v["request_id"], "checks:", v["verification"]["checked_checks"])'
  kill $srv 2>/dev/null
  trap - EXIT
else
  echo "SKIPPED: no server at $addr (set INTERVAL_ANALYZER_E2E=1 to auto-start one)"
  echo "        (the HTTP layer is still covered by tests/api_smoke.rs)"
fi

echo
echo "ALL CHECKS DONE"
