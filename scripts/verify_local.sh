#!/usr/bin/env bash
# Local end-to-end verification for ec-service.
#
# Starts the real Axum binary on an ephemeral port, stores a synthetic object,
# exercises missing / corrupt / beyond-tolerance cases over HTTP, and asserts
# concrete outcomes (status codes, categories, byte-identical payload).
#
# Usage: scripts/verify_local.sh
# Requires: curl, sha256sum, cargo (binary is built if absent).
set -euo pipefail

cd "$(dirname "$0")/.."

DATA_DIR="$(mktemp -d)"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
ADDR="127.0.0.1:${PORT}"
BASE="http://${ADDR}"
PASS=0; FAIL=0

log() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }

# assert_eq <description> <actual> <expected>
assert_eq() {
  if [ "$2" == "$3" ]; then ok "$1 (= $2)"; else bad "$1: got [$2], want [$3]"; fi
}
# assert_contains <description> <haystack> <needle>
assert_contains() {
  if [[ "$2" == *"$3"* ]]; then ok "$1"; else bad "$1: [$2] does not contain [$3]"; fi
}

cleanup() {
  [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$DATA_DIR"
}
trap cleanup EXIT

log "build"
cargo build --release 2>/dev/null

log "start server (data dir $DATA_DIR, port $PORT)"
EC_DATA_DIR="$DATA_DIR" EC_BIND_ADDR="$ADDR" RUST_LOG="info,ec_service=debug" \
  ./target/release/ec-service >"$DATA_DIR/server.log" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 50); do
  curl -sf "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 0.1
done

HJSON=$(curl -s "$BASE/healthz")
assert_contains "health reports field version" "$HJSON" "GF256-PP0x11D-G2/v1"
assert_contains "config exposes format version" "$(curl -s "$BASE/v1/config")" '"format_version":1'

# ----------------------------------------------------------------- encode
log "encode synthetic object (k=3, m=2)"
PAYLOAD="local-verification payload 0123456789-abcdef"
WANT_SHA=$(printf '%s' "$PAYLOAD" | sha256sum | cut -d' ' -f1)
PUT=$(curl -s -D "$DATA_DIR/put.h" -X PUT \
  "$BASE/v1/objects/demo?k=3&m=2" \
  --data-binary "$PAYLOAD")
assert_contains "PUT reports ok" "$PUT" '"ok":true'
assert_contains "PUT echoes payload sha256" "$PUT" "$WANT_SHA"
REQ_ID=$(grep -i '^x-request-id:' "$DATA_DIR/put.h" | tr -d '\r' | awk '{print $2}')
[ -n "$REQ_ID" ] && ok "PUT response carries x-request-id ($REQ_ID)" || bad "missing request id"

# files on disk: manifest + 5 shards
NSHARDS=$(find "$DATA_DIR/demo" -name 'shard-*.bin' | wc -l | tr -d ' ')
assert_eq "5 shard files exist" "$NSHARDS" "5"
[ -f "$DATA_DIR/demo/manifest.json" ] && ok "manifest.json present" || bad "manifest missing"

# ----------------------------------------------------------------- intact
INSP=$(curl -s "$BASE/v1/objects/demo/inspect")
assert_contains "fresh object intact" "$INSP" '"status":"intact"'

# exact download
GOT=$(curl -s "$BASE/v1/objects/demo")
assert_eq "downloaded payload byte-identical" "$GOT" "$PAYLOAD"

# ---------------------------------------------------------- missing shard 0
log "delete shard 0 (erasure)"
rm -f "$DATA_DIR/demo/shard-000.bin"
INSP=$(curl -s "$BASE/v1/objects/demo/inspect")
assert_contains "classified degraded_recoverable" "$INSP" '"status":"degraded_recoverable"'
assert_contains "missing list names 0" "$INSP" '"missing_shards":[0]'
assert_contains "margin 1 (4 verified - k 3)" "$INSP" '"margin":1'
GOT=$(curl -s "$BASE/v1/objects/demo")
assert_eq "payload recovered from 4 shards" "$GOT" "$PAYLOAD"

REP=$(curl -s -X POST "$BASE/v1/objects/demo/repair")
assert_contains "repair reports rebuilt shard 0" "$REP" '"rebuilt_shards":[0]'
assert_contains "repair post-verified intact" "$REP" '"status_after":"intact"'
INSP=$(curl -s "$BASE/v1/objects/demo/inspect")
assert_contains "object intact after repair" "$INSP" '"status":"intact"'

# ---------------------------------------------------------- corrupt shard 4
log "corrupt parity shard 4 (single byte flip)"
python3 - "$DATA_DIR/demo/shard-004.bin" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,'rb').read()); b[0]^=0x01; open(p,'wb').write(b)
PY
INSP=$(curl -s "$BASE/v1/objects/demo/inspect")
assert_contains "corruption classified corrupt (not missing)" "$INSP" '"corrupt_shards":[4]'
assert_contains "per-shard expected/computed digests shown" "$INSP" '"expected_sha256"'
GOT=$(curl -s "$BASE/v1/objects/demo")
assert_eq "payload recovered despite corrupt parity" "$GOT" "$PAYLOAD"
curl -s -X POST "$BASE/v1/objects/demo/repair" >/dev/null

# ------------------------------------------------------ missing+corrupt x2
log "missing shard 2 AND corrupt shard 3 (m=2 erasures, mixed)"
rm -f "$DATA_DIR/demo/shard-002.bin"
python3 - "$DATA_DIR/demo/shard-003.bin" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,'rb').read()); b[-1]^=0xff; open(p,'wb').write(b)
PY
GOT=$(curl -s "$BASE/v1/objects/demo")
assert_eq "exact payload with 2 mixed erasures" "$GOT" "$PAYLOAD"
curl -s -X POST "$BASE/v1/objects/demo/repair" >/dev/null

# ------------------------------------------------ beyond tolerance (3 > m)
log "remove/corrupt 3 shards (beyond m=2): service must refuse"
rm -f "$DATA_DIR/demo/shard-000.bin" "$DATA_DIR/demo/shard-002.bin"
python3 - "$DATA_DIR/demo/shard-004.bin" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,'rb').read()); b[0]^=0x5a; open(p,'wb').write(b)
PY
CODE=$(curl -s -o "$DATA_DIR/err.json" -w '%{http_code}' "$BASE/v1/objects/demo")
assert_eq "GET below k -> HTTP 409" "$CODE" "409"
assert_contains "error category NOT_RECOVERABLE" "$(cat "$DATA_DIR/err.json")" '"code":"NOT_RECOVERABLE"'
assert_contains "no fabricated data (detail lists need=3)" "$(cat "$DATA_DIR/err.json")" '"need":3'
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/objects/demo/repair")
assert_eq "repair below k -> HTTP 409" "$CODE" "409"

# ------------------------------------------------------------- error cases
log "named error categories"
assert_eq "unknown object -> 404" \
  "$(curl -s -o /dev/null -w '%{http_code}' "$BASE/v1/objects/ghost/inspect")" "404"
assert_eq "duplicate object -> 409" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/v1/objects/demo?k=3&m=2" --data-binary x)" "409"
# Both traversal-ish ids must fail. curl normalizes "/objects/.." to "/"
# client-side (404 at routing); the handler also rejects ".." with 400 for
# clients (e.g. a raw socket) that send it un-normalized.
assert_eq "path traversal id (curl-normalized) -> non-2xx" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/v1/objects/..?k=2&m=1" --data-binary x)" "404"
assert_eq "illegal object id (encoded slash) -> 400" \
  "$(curl -s -o /dev/null -w '%{http_code}' -X PUT "$BASE/v1/objects/a%2Fb?k=2&m=1" --data-binary x)" "400"

# request id correlation in logs
if grep -q "request_id=" "$DATA_DIR/server.log"; then
  ok "server logs correlate requests by request_id"
else
  bad "logs lack request_id correlation"
fi
grep -q "NOT recoverable" "$DATA_DIR/server.log" \
  && ok "uncertain conclusion (not recoverable) logged separately" \
  || bad "missing NOT recoverable log line"

# ------------------------------------------------------------- manifest tamper
log "tamper manifest original_len"
# restore object first
curl -s -X POST "$BASE/v1/objects/demo/repair" >/dev/null || true
# (object is beyond tolerance; create a fresh one to tamper with)
curl -s -X PUT "$BASE/v1/objects/t?k=2&m=1" --data-binary "ab" >/dev/null
python3 - "$DATA_DIR/t/manifest.json" <<'PY'
import json,sys
p=sys.argv[1]; m=json.load(open(p)); m["original_len"]=999
json.dump(m,open(p,"w"))
PY
CODE=$(curl -s -o "$DATA_DIR/m.json" -w '%{http_code}' "$BASE/v1/objects/t")
assert_eq "forged length -> HTTP 422" "$CODE" "422"
assert_contains "digest mismatch category" "$(cat "$DATA_DIR/m.json")" "MANIFEST_DIGEST_MISMATCH"

# ----------------------------------------------------------- empty payload
log "empty payload round trip"
curl -s -X PUT "$BASE/v1/objects/empty?k=2&m=1" --data-binary "" >/dev/null
[ "$(curl -s "$BASE/v1/objects/empty" | wc -c)" == "0" ] \
  && ok "empty object returns 0 bytes" || bad "empty object round trip"

printf '\n\033[1;33mRESULT: %d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
