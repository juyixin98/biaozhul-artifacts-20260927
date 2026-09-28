#!/usr/bin/env bash
# Run every test suite and print a machine- and human-readable summary.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== go vet =="
go vet ./...

echo
echo "== go test (race detector) =="
go test -race -count=1 ./...

echo
echo "All suites passed."
