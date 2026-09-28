#!/usr/bin/env bash
# End-to-end smoke + failure-injection test against a real running server.
# Uses only: bash, curl, coreutils base64, python3 (no jq dependency).
#
# Verifies, in order:
#   1. health + request id echo
#   2. encode an object, decode it back byte-for-byte
#   3. remove one shard file            -> verify says "missing", decode OK
#   4. corrupt another shard file       -> verify says "bad_digest", decode OK
#   5. remove a third (m+1 total loss)  -> decode 409 INSUFFICIENT_SHARDS, no data
#   6. repair all                       -> verify all good
#   7. cross-implementation: the independent Python oracle recovers the
#      Rust-produced on-disk object and returns the identical bytes.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8080}"
BASE="http://127.0.0.1:${PORT}"
STORE="$(mktemp -d)"
CFG="$(mktemp --suffix=.json)"
trap 'kill $SRV_PID 2>/dev/null || true; rm -rf "$STORE" "$CFG"' EXIT

cat > "$CFG" <<EOF
{"listen":"127.0.0.1:${PORT}","storage_dir":"${STORE}","allow_writes":true,"k":3,"m":2,"log_level":"warn"}
EOF

# tiny JSON field reader without jq; non-string values emitted as JSON
# (so booleans print true/false, numbers bare)
jget() {
  python3 -c '
import sys,json
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d=d.get(k) if isinstance(d,dict) else None
if isinstance(d,str): print(d)
elif d is None: print("")
else: print(json.dumps(d))' "$1"
}

echo "== starting server =="
cargo run --offline -q -p ec-server -- --config "$CFG" &
SRV_PID=$!
for _ in $(seq 1 50); do curl -sf "$BASE/health" >/dev/null && break; sleep 0.2; done

echo "== [1] health + request id =="
RIDHDR=$(curl -s -D - -o /dev/null -H 'x-request-id: e2e-rid-1' "$BASE/health" | tr -d '\r')
echo "$RIDHDR" | grep -qi 'x-request-id: e2e-rid-1' && echo "OK request id echoed" || { echo FAIL; exit 1; }

echo "== [2] encode =="
ORIGINAL="erasure-coding end-to-end verification payload!!"
ORIGINAL_LEN=${#ORIGINAL}
B64=$(printf '%s' "$ORIGINAL" | base64 -w0)
ENC=$(curl -s -X POST "$BASE/v1/objects" -H 'content-type: application/json' \
  -d "{\"object_id\":\"e2e-obj\",\"data_b64\":\"$B64\"}")
echo "$ENC" | jget ok | grep -qx true
OLEN=$(echo "$ENC" | jget result.original_len)
[ "$OLEN" = "$ORIGINAL_LEN" ] && echo "OK original_len=${ORIGINAL_LEN}" || { echo "FAIL original_len=$OLEN"; exit 1; }

echo "== [2b] healthy decode byte-for-byte =="
DEC=$(curl -s -X POST "$BASE/v1/objects/e2e-obj/decode")
GOT=$(echo "$DEC" | jget result.data_b64 | base64 -d)
[ "$GOT" = "$ORIGINAL" ] && echo "OK decoded matches original" || { echo "FAIL decode mismatch"; exit 1; }

echo "== [3] one missing shard (index 1) =="
rm -f "$STORE/e2e-obj/shards/shard-00001.bin"
V=$(curl -s "$BASE/v1/objects/e2e-obj/verify")
echo "$V" | jget result.missing_indices | grep -q '1' && echo "OK reported missing=[1]" || { echo FAIL; exit 1; }
DEC=$(curl -s -X POST "$BASE/v1/objects/e2e-obj/decode")
GOT=$(echo "$DEC" | jget result.data_b64 | base64 -d)
[ "$GOT" = "$ORIGINAL" ] && echo "OK recovered despite missing shard" || { echo FAIL; exit 1; }

echo "== [4] one bad-digest shard (flip byte in index 4) =="
python3 - "$STORE/e2e-obj/shards/shard-00004.bin" <<'PY'
import sys
p=sys.argv[1]; b=bytearray(open(p,'rb').read()); b[0]^=0xFF; open(p,'wb').write(b)
PY
V=$(curl -s "$BASE/v1/objects/e2e-obj/verify")
echo "$V" | jget result.bad_digest_indices | grep -q '4' && echo "OK reported bad_digest=[4]" || { echo FAIL; exit 1; }
DEC=$(curl -s -X POST "$BASE/v1/objects/e2e-obj/decode")
GOT=$(echo "$DEC" | jget result.data_b64 | base64 -d)
[ "$GOT" = "$ORIGINAL" ] && echo "OK recovered treating bad shard as erasure" || { echo FAIL; exit 1; }

echo "== [5] beyond tolerance on a SEPARATE object (3 erasures, only 2 usable) =="
DEC2=$(curl -s -X POST "$BASE/v1/objects" -H 'content-type: application/json' \
  -d "{\"data_b64\":\"$B64\"}")
OID2=$(echo "$DEC2" | jget result.object_id)
rm -f "$STORE/$OID2/shards/shard-00000.bin" \
      "$STORE/$OID2/shards/shard-00002.bin" \
      "$STORE/$OID2/shards/shard-00004.bin"
CODE=$(curl -s -o /tmp/ec-dec-fail.json -w '%{http_code}' -X POST "$BASE/v1/objects/$OID2/decode")
[ "$CODE" = "409" ] && echo "OK HTTP 409" || { echo "FAIL status=$CODE"; exit 1; }
python3 - <<'PY'
import json
d=json.load(open('/tmp/ec-dec-fail.json'))
assert d["ok"] is False
assert d["error"]["code"] == "INSUFFICIENT_SHARDS", d
# The refusal response must contain NO reconstructed bytes anywhere.
s=json.dumps(d)
assert "data_b64" not in s, "fabricated data leaked into refusal response"
print("OK exact category and no fabricated data")
PY

echo "== [6] repair main object (missing=[1], bad_digest=[4], exactly m=2) =="
R=$(curl -s -X POST "$BASE/v1/objects/e2e-obj/repair" -H 'content-type: application/json' -d '{"object_id":"e2e-obj","targets":[]}')
echo "$R" | jget result.repaired | grep -qx true && echo "OK repaired=true" || { echo FAIL; exit 1; }
V=$(curl -s "$BASE/v1/objects/e2e-obj/verify")
GC=$(echo "$V" | jget result.good_count)
[ "$GC" = "5" ] && echo "OK all 5 shards good after repair" || { echo "FAIL good_count=$GC"; exit 1; }
DEC=$(curl -s -X POST "$BASE/v1/objects/e2e-obj/decode")
GOT=$(echo "$DEC" | jget result.data_b64 | base64 -d)
[ "$GOT" = "$ORIGINAL" ] && echo "OK post-repair decode matches" || { echo FAIL; exit 1; }

echo "== [7] cross-implementation recovery (Python oracle reads Rust store) =="
python3 tests/reference/oracle.py recover \
  --manifest "$STORE/e2e-obj/manifest.json" \
  --shards "$STORE/e2e-obj/shards" > /tmp/ec-oracle.json
python3 - "$ORIGINAL" <<'PY'
import sys,json,base64
want=sys.argv[1].encode()
got=base64.b64decode(json.load(open('/tmp/ec-oracle.json'))["original_b64"])
assert got==want, "independent Python recovery mismatched Rust on-disk object"
print("OK independent Python oracle recovered identical", len(got), "bytes")
PY

echo
echo "ALL END-TO-END CHECKS PASSED"
