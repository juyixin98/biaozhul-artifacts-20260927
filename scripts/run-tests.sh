#!/usr/bin/env bash
# Runs the complete verification suite: unit + integration + race detector.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== go vet =="
go vet ./...

echo "== unit + integration tests =="
go test ./... -count=1

echo "== race detector =="
go test -race ./... -count=1

echo
echo "ALL CHECKS PASSED"
