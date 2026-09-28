#!/usr/bin/env bash
# End-to-end verification for the hierarchical bitmap set project.
#
# Steps:
#   1. cargo build + workspace tests
#   2. clippy (warnings treated as failures)
#   3. deterministic fixture generation
#   4. CLI verify/inspect on a generated fixture
#   5. HTTP smoke test against a live server (build, rank, select, algebra,
#      cross-check, corrupt-file 422 category, request-id correlation)
#
# Exit code is non-zero on the first failed step.
set -euo pipefail

cd "$(dirname "$0")/.."
BASE="$PWD"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DATA="$WORK/data"
FIX="$WORK/fixtures"
mkdir -p "$DATA"
PORT="${HBS_SMOKE_PORT:-18080}"
ADDR="127.0.0.1:$PORT"
export HBS_DATA_DIR="$DATA"
export HBS_BIND_ADDR="$ADDR"

step() { printf '\n=== %s ===\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

step "1/5 build"
cargo build --workspace

step "2/5 tests + clippy"
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings

step "3/5 generate deterministic fixtures"
cargo run -q -p hbs-cli -- gen-fixtures --out "$FIX"
test -f "$FIX/sparse-seed1.values.json"
test -f "$FIX/dense-seed1.hbs"
test -f "$FIX/container_threshold-seed1.meta.json"
# Ground-truth values file exists independently of the encoded SUT file.
# Threshold fixture: two array chunks (4095, 4096) + one bitmap chunk (4097).
grep -q '"array_containers": 2' "$FIX/container_threshold-seed1.meta.json"
grep -q '"bitmap_containers": 1' "$FIX/container_threshold-seed1.meta.json"

step "4/5 CLI verify + inspect"
# Copy one fixture into the store data dir and verify it.
cp "$FIX/dense-seed1.hbs" "$DATA/demo.hbs"
cargo run -q -p hbs-cli -- verify --data-dir "$DATA" demo
cargo run -q -p hbs-cli -- inspect --data-dir "$DATA" demo
# Corrupt one byte -> exit code 4 (corruption category).
printf '\x00' >> "$DATA/demo.hbs"
if cargo run -q -p hbs-cli -- verify --data-dir "$DATA" demo >/dev/null 2>&1; then
  fail "corrupted file was accepted"
fi
echo "corruption correctly rejected (exit nonzero)"

step "5/5 live HTTP smoke test"
# Restore a valid file for the API tests.
cp "$FIX/interleaved-seed42.hbs" "$DATA/inter.hbs"
cargo run -q -p hbs-cli -- serve --config /dev/null >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
trap 'kill $SERVER_PID 2>/dev/null || true; rm -rf "$WORK"' EXIT

# Wait for the port.
for i in $(seq 1 50); do
  if curl -sf "http://$ADDR/api/v1/health" -H 'X-Request-Id: boot' >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

curl_json() { curl -s -H 'Content-Type: application/json' "$@"; }

# request-id correlation
BODY=$(curl_json "http://$ADDR/api/v1/health" -H 'X-Request-Id: rid-abc')
echo "$BODY" | grep -q '"request_id":"rid-abc"' || fail "request id not echoed: $BODY"

# create a set with known values
curl_json -X POST "http://$ADDR/api/v1/sets" -d '{"name":"nums","values":[0,1,2,100,65536,65537,4294967295]}' \
  | grep -q '"ok":true' || fail "create failed"

# rank/select concrete answers
curl_json -X POST "http://$ADDR/api/v1/sets/nums/rank" -d '{"value":100,"inclusive":true}' \
  | grep -q '"rank":4' || fail "rank_le(100) != 4"
curl_json -X POST "http://$ADDR/api/v1/sets/nums/select" -d '{"rank":0}' \
  | grep -q '"value":0' || fail "select(0) != 0"
curl_json -X POST "http://$ADDR/api/v1/sets/nums/select" -d '{"rank":6}' \
  | grep -q '"value":4294967295' || fail "select(6) != u32::MAX"

# algebra with persistence
curl_json -X POST "http://$ADDR/api/v1/sets" -d '{"name":"other","values":[2,100,999]}' >/dev/null
curl_json -X POST "http://$ADDR/api/v1/algebra/intersection" \
  -d '{"left":"nums","right":"other","save_as":"inter_set"}' \
  | grep -q '"cardinality":2' || fail "intersection cardinality != 2"

# independent oracle cross-check
curl_json -X POST "http://$ADDR/api/v1/verify/cross-check" \
  -d '{"a":[1,2,3,65536],"b":[2,3,4,65537]}' \
  | grep -q '"passed":true' || fail "oracle cross-check did not pass"

# fixture distribution sweep
curl_json -X POST "http://$ADDR/api/v1/fixtures" -d '{"distribution":"all"}' \
  | grep -q '"passed":true' || fail "fixture sweep failed"

# corrupt the on-disk set: verify endpoint returns a specific 422 category
python3 - "$DATA/inter.hbs" <<'PY'
import sys
p = sys.argv[1]
b = bytearray(open(p, 'rb').read())
b[len(b)//2] ^= 0x01
open(p, 'wb').write(b)
PY
CODE=$(curl -s -o "$WORK/err.json" -w '%{http_code}' "http://$ADDR/api/v1/sets/inter/verify")
test "$CODE" = "422" || fail "expected 422, got $CODE"
grep -q 'corrupt_checksum' "$WORK/err.json" || fail "expected corrupt_checksum category"
echo "corrupt file rejected with HTTP 422 corrupt_checksum"

# not-found category
CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://$ADDR/api/v1/sets/does-not-exist")
test "$CODE" = "404" || fail "expected 404, got $CODE"

kill $SERVER_PID 2>/dev/null || true
echo
echo "ALL VERIFICATION STEPS PASSED"
