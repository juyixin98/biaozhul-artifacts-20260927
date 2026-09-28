#!/usr/bin/env bash
# Replay normal and abnormal LZ77B service calls.
# Usage: ./examples/http-calls.sh [base-url]
set -u
BASE="${1:-http://127.0.0.1:18080}"
SID="demo-$(date +%s)"
b64() { printf '%s' "$1" | base64 -w0; }
jget() { python3 -c 'import sys,json;d=json.load(sys.stdin);print(*[d.get(k) for k in sys.argv[1:]])' "$@"; }

say() { printf '\n=== %s ===\n' "$1"; }

say "healthz (run id propagated)"
curl -s -H 'X-Run-Id: ex-health' "$BASE/healthz"; echo

say "create stream $SID"
curl -s -H 'X-Run-Id: ex-create' -H 'Content-Type: application/json' \
  -d "{\"stream_id\":\"$SID\"}" "$BASE/v1/streams"; echo

say "encode independent block 0"
RESP=$(curl -s -H 'X-Run-Id: ex-enc0' -H 'Content-Type: application/json' \
  -d "{\"data\":\"$(b64 'alpha alpha alpha alpha alpha alpha')\"}" \
  "$BASE/v1/streams/$SID/encode")
echo "$RESP" | python3 -m json.tool | grep -v '"block"'
BLOCK0=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["block"])')

say "encode dependent block 1 (reuses predecessor dictionary)"
curl -s -H 'X-Run-Id: ex-enc1' -H 'Content-Type: application/json' \
  -d "{\"data\":\"$(b64 'alpha again alpha again')\"}" \
  "$BASE/v1/streams/$SID/encode" | python3 -m json.tool | grep -v '"block"'

say "decode whole stream"
curl -s -H 'X-Run-Id: ex-dec' "$BASE/v1/streams/$SID/decode" | python3 -c '
import sys,json,base64
d=json.load(sys.stdin); print("bytes:",d["bytes"]); print(base64.b64decode(d["data"]))'

say "stateless decode with independent-reference cross check"
curl -s -H 'Content-Type: application/json' \
  -d "{\"block\":\"$BLOCK0\",\"cross_check\":true}" "$BASE/v1/decode" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);print("ok=",d["ok"],"cross=",d["cross_check"])'

say "400 input: malformed JSON"
curl -s -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' -d '{bad' \
  "$BASE/v1/encode-independent"

say "409 state: dependent block appended without predecessor"
# A dependent block at index 1 is generated on demand with the local CLI so
# this script stays self-contained for a fresh reviewer.
F="/tmp/c16/block-00000001.lzb"
if [ ! -f "$F" ]; then
  ./target/release/lz77b chain fixtures/sample1.bin /tmp/c16 16 >/dev/null 2>&1 \
    || cargo run --release --offline --bin lz77b -- chain fixtures/sample1.bin /tmp/c16 16 >/dev/null 2>&1
fi
if [ -f "$F" ]; then
  B=$(base64 -w0 "$F")
  curl -s -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' \
    -d "{\"block\":\"$B\"}" "$BASE/v1/streams/never-created/blocks"
else
  echo "(skip: could not build dependent block)"
fi

say "413 resource: 4 GiB output bomb in header"
BOMB=$(python3 -c '
import base64,struct
h=b"LZ7B"+bytes([1,0])+struct.pack(">I",0)+struct.pack(">Q",0)+struct.pack(">I",0)+struct.pack(">Q",4*1024*1024*1024)+bytes([2])
print(base64.b64encode(h).decode())')
curl -s -w '\nHTTP %{http_code}\n' -H 'Content-Type: application/json' \
  -d "{\"block\":\"$BOMB\"}" "$BASE/v1/decode"

say "404 state/not-found: decode a stream that does not exist"
curl -s -w '\nHTTP %{http_code}\n' "$BASE/v1/streams/never-existed/decode"
