#!/usr/bin/env bash
# End-to-end smoke test against a locally running hcompd.
#
# Usage:
#   scripts/e2e.sh [base_url]
#
# Requires: curl, python3. The server must already be running, e.g.
#   HCOMP_BIND_ADDR=127.0.0.1:8787 \
#   HCOMP_STORE_DIR=./data ./target/release/hcompd
#
# Exits non-zero on the first failed expectation. Prints PASS lines.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8787}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

# health
[ "$(curl -sS "$BASE/healthz")" = "ok" ] || fail "healthz"
pass "healthz"

# roundtrip
printf 'canonical huffman demo data aaaaa bbbbb' > "$TMP/in.txt"
curl -sS -X POST "$BASE/api/v1/encode" --data-binary @"$TMP/in.txt" \
     -o "$TMP/in.hcmp" -D "$TMP/h.txt"
grep -qi 'x-run-id:' "$TMP/h.txt" || fail "x-run-id response header"
pass "encode returns x-run-id"
[ "$(head -c4 "$TMP/in.hcmp")" = "HCMP" ] || fail "container magic"
pass "encoded container has HCMP magic"

curl -sS -X POST "$BASE/api/v1/verify" --data-binary @"$TMP/in.hcmp" \
     | grep -q '"ok":true' || fail "verify"
pass "verify ok"

curl -sS -X POST "$BASE/api/v1/decode" --data-binary @"$TMP/in.hcmp" \
     -o "$TMP/out.txt"
cmp "$TMP/in.txt" "$TMP/out.txt" || fail "stateless roundtrip"
pass "stateless roundtrip byte-identical"

# object lifecycle
curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/v1/objects" \
     -H 'x-object-id: demo' --data-binary @"$TMP/in.txt" \
     | grep -q 201 || fail "create object 201"
pass "create object -> 201"
curl -sS -o /dev/null -w '%{http_code}' -X PUT "$BASE/api/v1/objects" \
     -H 'x-object-id: demo' --data-binary @"$TMP/in.txt" \
     | grep -q 409 || fail "duplicate object 409"
pass "duplicate create -> 409"
curl -sS "$BASE/api/v1/objects/demo" -o "$TMP/obj.bin"
cmp "$TMP/in.txt" "$TMP/obj.bin" || fail "object get"
pass "GET object byte-identical"
curl -sS -o /dev/null -X DELETE "$BASE/api/v1/objects/demo"
pass "object lifecycle"

# unknown version must be rejected with the exact category
python3 - "$TMP/in.hcmp" "$TMP/badver.bin" <<'PY'
import sys
b = bytearray(open(sys.argv[1],'rb').read()); b[4] = 99
open(sys.argv[2],'wb').write(b)
PY
code=$(curl -sS -o "$TMP/err.json" -w '%{http_code}' -X POST "$BASE/api/v1/decode" \
     --data-binary @"$TMP/badver.bin" || true)
[ "$code" = "422" ] || fail "unknown version status $code"
grep -q '"error":"unknown_version"' "$TMP/err.json" || fail "unknown version category"
pass "unknown version -> 422 unknown_version"

echo "ALL E2E CHECKS PASSED ($BASE)"
