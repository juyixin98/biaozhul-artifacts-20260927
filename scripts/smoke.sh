#!/usr/bin/env bash
# End-to-end smoke test for the placement backend using only the real binary,
# a temp SQLite DB and curl. Prints PASS/FAIL per scenario and exits non-zero
# on any mismatch. No external services required.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$ROOT/bin/placer"
DB="$(mktemp -u /tmp/placer-smoke.XXXXXX.db)"
PORT="${PLACER_SMOKE_PORT:-18091}"
BASE="http://127.0.0.1:${PORT}"
LOG="$(mktemp)"
PASS=0; FAIL=0

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

check() { # desc, expected, actual
  if [ "$2" = "$3" ]; then green "PASS: $1"; PASS=$((PASS+1));
  else red "FAIL: $1 (expected [$2] got [$3])"; FAIL=$((FAIL+1)); fi
}

cleanup() { [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null; rm -f "$DB" "$DB-wal" "$DB-shm" "$LOG"; }
trap cleanup EXIT

( cd "$ROOT" && "$BIN" --config configs/placer.json --fixture test/testdata/tight.json \
    --db "$DB" --addr ":${PORT}" >"$LOG" 2>&1 ) &
PID=$!

# Wait for readiness (max ~5s).
for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

# 1) health
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/healthz")
check "healthz returns 200" 200 "$code"

# 2) feasible spread placement (hard zone anti-affinity, two zones)
resp=$(curl -s -X POST "$BASE/v1/plans" -H 'X-Run-Id: smoke-spread' -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a1","zone":"za","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":10000000000,"storage_bytes":100000000000}},
           {"id":"b1","zone":"zb","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":10000000000,"storage_bytes":100000000000}}],
  "instances":[{"id":"i1","state":"pending","request":{"milli_cpu":500,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}},
               {"id":"i2","state":"pending","request":{"milli_cpu":500,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}}],
  "policy":{"groups":[{"group":"app","mode":"hard","affinity":false,"topology_key":"zone"}]}}')
feasible=$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["feasible"])' 2>/dev/null)
spread=$(printf '%s' "$resp" | python3 -c '
import sys,json
d=json.load(sys.stdin)
m={x["instance_id"]:x["node_id"] for x in d["decisions"]}
print("different" if m.get("i1")!=m.get("i2") else "same")' 2>/dev/null)
check "feasible plan" True "$feasible"
check "i1 and i2 spread across distinct zones" "different" "$spread"

# 3) no-solution is a 409 conflict, not 2xx
http=$(curl -s -o /tmp/smoke-conflict.json -w '%{http_code}' -X POST "$BASE/v1/plans" \
  -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a1","zone":"za","status":"ready","capacity":{"milli_cpu":100,"memory_bytes":1,"storage_bytes":1}}],
  "instances":[{"id":"big","state":"pending","request":{"milli_cpu":9000,"memory_bytes":1,"storage_bytes":1}}]}')
check "infeasible plan returns 409" 409 "$http"
ccode=$(python3 -c 'import json;print(json.load(open("/tmp/smoke-conflict.json"))["conflicts"][0]["code"])' 2>/dev/null)
check "conflict category is insufficient_resources" insufficient_resources "$ccode"
rm -f /tmp/smoke-conflict.json

# 4) mutual anti-affinity with one usable zone -> 409 + anti_affinity
http=$(curl -s -o /tmp/smoke-aa.json -w '%{http_code}' -X POST "$BASE/v1/plans" \
  -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a1","zone":"za","status":"ready","capacity":{"milli_cpu":8000,"memory_bytes":10000000000,"storage_bytes":100000000000}},
           {"id":"b1","zone":"zb","status":"disabled","capacity":{"milli_cpu":8000,"memory_bytes":10000000000,"storage_bytes":100000000000}}],
  "instances":[{"id":"x","state":"pending","request":{"milli_cpu":100,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}},
               {"id":"y","state":"pending","request":{"milli_cpu":100,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}}],
  "policy":{"groups":[{"group":"app","mode":"hard","affinity":false,"topology_key":"zone"}]}}')
check "mutual anti-affinity returns 409" 409 "$http"
aacode=$(python3 -c '
import json
d=json.load(open("/tmp/smoke-aa.json"))
print("\"code\": \"anti_affinity_conflict\"" if any(c["code"]=="anti_affinity_conflict" for c in d["conflicts"]) else "")' 2>/dev/null)
check "anti-affinity category present" '"code": "anti_affinity_conflict"' "$aacode"
rm -f /tmp/smoke-aa.json

# 5) rolling replacement feasible and starts with surge into spare zone
resp=$(curl -s -X POST "$BASE/v1/replacements" -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a1","zone":"za","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":10000000000,"storage_bytes":100000000000}},
           {"id":"b1","zone":"zb","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":10000000000,"storage_bytes":100000000000}},
           {"id":"c1","zone":"zc","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":10000000000,"storage_bytes":100000000000}}],
  "old":[{"id":"o1","state":"bound","node_id":"a1","request":{"milli_cpu":800,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}},
         {"id":"o2","state":"bound","node_id":"b1","request":{"milli_cpu":800,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}}],
  "new":[{"id":"n1","state":"pending","request":{"milli_cpu":800,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}},
         {"id":"n2","state":"pending","request":{"milli_cpu":800,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}}],
  "replaces":{"n1":"o1","n2":"o2"},"max_surge":1,"max_unavailable":0,
  "policy":{"groups":[{"group":"app","mode":"hard","affinity":false,"topology_key":"zone"}]}}')
first_op=$(printf '%s' "$resp" | python3 -c '
import sys,json
d=json.load(sys.stdin)
print("\"kind\": \"%s\"" % d["ops"][0]["kind"]) if d.get("ops") else print("")' 2>/dev/null)
check "rolling first op is place_new (surge into spare zone)" '"kind": "place_new"' "$first_op"

# 6) malformed JSON -> 400 (never a success)
http=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/plans" \
  -H 'Content-Type: application/json' -d '{"bogus":1}')
check "malformed request returns 400" 400 "$http"

echo
if [ "$FAIL" -eq 0 ]; then green "ALL SMOKE CHECKS PASSED ($PASS)"; else red "$FAIL SMOKE CHECK(S) FAILED ($PASS passed)"; fi
exit "$FAIL"
