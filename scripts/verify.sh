#!/usr/bin/env bash
# Local verification for the offline IPv4 fragment-reassembly backend.
# No network traffic is sent; everything runs against synthetic PCAP
# fixtures and a local SQLite file.
set -euo pipefail

cd "$(dirname "$0")/.."
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== go version =="
go version

echo "== 1/5 build =="
go build ./...
echo "build OK"

echo "== 2/5 vet =="
go vet ./...
echo "vet OK"

echo "== 3/5 regenerate fixtures (deterministic; manifest must not change) =="
cp testdata/manifest.json "$WORK/manifest.before.json"
go run ./cmd/genfixture -out testdata
if ! cmp -s testdata/manifest.json "$WORK/manifest.before.json"; then
  echo "FAIL: regenerated manifest differs from committed testdata/manifest.json" >&2
  diff "$WORK/manifest.before.json" testdata/manifest.json || true
  exit 1
fi
echo "fixtures deterministic"

echo "== 4/5 unit + integration tests (verbose, correlated run ids) =="
go test ./... -count=1 -v 2>&1 | tee "$WORK/test.log" | grep -E '^(=== RUN|--- (PASS|FAIL)|ok|FAIL|PASS)' | grep -vE '=== RUN' | head -80
if grep -qE '^FAIL' "$WORK/test.log"; then
  echo "FAIL: some tests failed (full log: $WORK/test.log)" >&2
  exit 1
fi

echo "== 5/5 CLI golden verification of every fixture =="
rc=0
for f in basic overlap conflict_last badlen timeout_reuse; do
  if go run ./cmd/reasm-cli \
       -config config/reasm.json \
       -pcap "testdata/$f.pcap" \
       -run "verify-$f" \
       -db "$WORK/$f.db" \
       -expect testdata/manifest.json; then
    echo "  $f: PASS"
  else
    echo "  $f: FAIL"
    rc=1
  fi
done

if [ "$rc" -ne 0 ]; then
  exit 1
fi
echo
echo "ALL VERIFICATION STEPS PASSED"
